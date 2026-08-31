"""
dot.coding.extensions.runtime — ExtensionRuntime

管理扩展生命周期、事件分发、fail-closed hook 链。

关键设计（来自 Tau 的洞察）：
- ExtensionGeneration liveness token：每次 /reload 创建新 generation，旧引用立即失效
- fail-closed hook 链：hook 异常时默认阻止工具执行（block=True）
- _notify 拷贝监听器列表：防止回调中取消订阅时的"迭代中修改集合"异常
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable, signature
from pathlib import Path
from typing import Any

from dot.agent.events import AgentEvent
from dot.agent.tools import AgentTool, AgentToolResult
from dot.ai.types import ToolCall

from .api import ExtensionContext, ExtensionAPI
from .generation import ExtensionError, ExtensionGeneration
from .loader import ExtensionLoader, ExtensionModule

logger = logging.getLogger(__name__)

HookFn = Callable[..., Awaitable[None] | None]
EventListener = Callable[[AgentEvent], Awaitable[None] | None]

# hook 时机全集：
#   before_tool_call  — 工具执行前（可阻止）：fn(tool_call) -> (blocked, reason) | None
#   after_tool_call   — 工具执行后（可改写结果）：fn(tool_call, result, is_error) -> (result, is_error) | None
#   agent_start / agent_end / turn_start / turn_end /
#   message_start / message_end /
#   tool_execution_start / tool_execution_update / tool_execution_end
#                     — 生命周期观察点：fn(event)，返回值忽略
HOOK_TIMINGS = (
    "before_tool_call", "after_tool_call",
    "agent_start", "agent_end", "turn_start", "turn_end",
    "message_start", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
)

# AgentEvent 类 -> 生命周期 hook 时机名
_EVENT_TO_TIMING = {
    "AgentStartEvent": "agent_start", "AgentEndEvent": "agent_end",
    "TurnStartEvent": "turn_start", "TurnEndEvent": "turn_end",
    "MessageStartEvent": "message_start", "MessageEndEvent": "message_end",
    "MessageUpdateEvent": "message_update",
    "ToolExecutionStartEvent": "tool_execution_start",
    "ToolExecutionUpdateEvent": "tool_execution_update",
    "ToolExecutionEndEvent": "tool_execution_end",
}


def timing_for_event(event: AgentEvent) -> str | None:
    """AgentEvent -> hook 时机名（未知事件返回 None）"""
    return _EVENT_TO_TIMING.get(type(event).__name__)


@dataclass
class HookEntry:
    """一个注册的 hook（matcher 对工具时机匹配工具名，对生命周期时机匹配时机名）"""
    name: str
    timing: str
    fn: HookFn
    matcher: Any  # re.Pattern
    extension_name: str = ""


@dataclass
class EventSubscription:
    """一个事件监听"""
    event_type: str
    fn: EventListener
    extension_name: str = ""


class ExtensionRuntime:
    """扩展运行时

    职责：
      - 管理扩展生命周期（load / reload / unload）
      - 事件分发给订阅者
      - 执行 tool_call / tool_result hook 链
      - 管理工具/命令/提示词注册
    """

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        extra_dirs: list[Path] | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self._loader = ExtensionLoader(extra_dirs=extra_dirs)
        self._generation = ExtensionGeneration()

        # 注册表
        self._tools: dict[str, AgentTool] = {}
        self._commands: dict[str, Callable[..., Any]] = {}
        self._prompt_sections: list[str] = []

        # Hook 链：按时机组织（before_tool_call / after_tool_call / agent_start / ...）
        self._hooks: dict[str, list[HookEntry]] = {timing: [] for timing in HOOK_TIMINGS}

        # 事件监听
        self._event_listeners: list[EventSubscription] = []

        # 运行时依赖（BuiltInExtensionContext / ExtensionContext 使用）
        # 用来存放运行时注入的依赖。外部代码可以通过 set_runtime_dep(key, value) 往里面塞东西。
        # 注入的时 AgentHost对象
        self._runtime_deps: dict[str, Any] = {}

        # 已加载模块及其上下文（reload 时用于 teardown）
        self._loaded: list[tuple[ExtensionModule, ExtensionContext]] = []

        # harness 事件订阅的解绑函数（attach_to_harness 时赋值）
        self._harness_unsub: Callable[[], None] | None = None

    @property
    def generation(self) -> ExtensionGeneration:
        return self._generation

    @property
    def tools(self) -> list[AgentTool]:
        return list(self._tools.values())

    @property
    def commands(self) -> dict[str, Callable[..., Any]]:
        return dict(self._commands)

    @property
    def prompt_sections(self) -> list[str]:
        return list(self._prompt_sections)

    # ============================================================
    # 生命周期
    # ============================================================

    def load(self) -> int:
        """扫描并加载所有扩展，返回成功加载数"""
        self._loader.scan()
        # 清空注册表
        self._tools.clear()
        self._commands.clear()
        self._prompt_sections.clear()
        for entries in self._hooks.values():
            entries.clear()
        self._event_listeners.clear()
        self._loaded.clear()

        loaded = 0
        # _RuntimeAPI(self, ...) — 创建注册器 ext
        api = _RuntimeAPI(self, self._generation)
        for name, module in self._loader.modules.items():
            if not module.is_loadable:
                logger.warning("[extensions] Skipping %s: %s", name, module.load_error)
                continue
            try:
                # _build_context(name) — 创建上下文 ctx
                ctx = self._build_context(name)
                # self._call_with_context(module.setup_fn, api, ctx) — 调用 setup(ext) 或 setup(ext, ctx)
                self._call_with_context(module.setup_fn, api, ctx)
                self._loaded.append((module, ctx))
                loaded += 1
                logger.info("[extensions] Loaded: %s", name)
            except Exception as exc:
                logger.error("[extensions] Error loading %s: %s", name, exc)

        return loaded

    def reload(self) -> int:
        """热重载：先 teardown 旧扩展，再使旧 generation 失效，重新扫描加载"""
        self._teardown_all()
        old_generation = self._generation
        self._generation = ExtensionGeneration()
        old_generation.invalidate()
        return self.load()

    def _teardown_all(self) -> None:
        """逐个调用已加载扩展的 teardown（可选），单失败不中断"""
        for module, ctx in self._loaded:
            if module.teardown_fn is None:
                continue
            try:
                self._call_with_context(module.teardown_fn, None, ctx)
            except Exception as exc:
                logger.error("[extensions] teardown %s error: %s", module.name, exc)
        self._loaded.clear()

    @staticmethod
    def _call_with_context(fn: Callable[..., Any], api: Any, ctx: ExtensionContext) -> Any:
        """按参数个数调用扩展入口：setup(ext, ctx) / setup(ext) / setup()

        这个fn 就是 扩展py文件中的 setup 函数或 teardown 函数
        api 是 运行时注册器 ext
        ctx 是 扩展上下文，包含 workspace / host / config / 专用 logger / 依赖表
        teardown 同理（api 传 None）。参数个数通过签名内省判定，兼容两种写法。
        """
        try:
            positional = [
                p for p in signature(fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            count = len(positional)
        except (TypeError, ValueError):
            count = 2
        if count >= 2:
            return fn(api, ctx)
        if count == 1:
            return fn(api if api is not None else ctx)
        return fn()

    def _build_context(self, extension_name: str) -> ExtensionContext:
        """构建扩展上下文：workspace / host / config / 专用 logger / 依赖表"""
        import logging as _logging

        return ExtensionContext(
            workspace=self.workspace,
            extension_name=extension_name,
            host=self._runtime_deps.get("host"),
            # 可以给扩展传递自定义配置参数 self.extensions.set_runtime_dep("config", {"max_retries": 3})
            config=self._runtime_deps.get("config", {}),
            logger=_logging.getLogger(f"dot.ext.{extension_name}"),
            _deps=dict(self._runtime_deps),
        )

    # ============================================================
    # Hook 链（fail-closed）
    # ============================================================

    async def run_tool_call_hooks(
        self,
        tool_call: ToolCall,
    ) -> tuple[bool, str | None]:
        """执行 before_tool_call hook 链（工具执行前）

        按注册顺序执行 matcher 命中（正则匹配工具名）的 hook；
        hook 返回 (True, reason) 阻止执行。
        fail-closed：hook 异常时默认阻止工具执行。
        """
        for entry in list(self._hooks["before_tool_call"]):
            if not entry.matcher.search(tool_call.name or ""):
                continue
            try:
                result = entry.fn(tool_call)
                if isawaitable(result):
                    result = await result
                if isinstance(result, tuple) and len(result) == 2 and result[0]:
                    return True, result[1] or f"Blocked by hook '{entry.name}'"
            except Exception as exc:
                logger.error(
                    "[extensions] before_tool_call hook '%s' error (fail-closed): %s",
                    entry.name, exc,
                )
                return True, f"Hook '{entry.name}' raised an error: {exc}"
        return False, None

    async def run_tool_result_hooks(
        self,
        tool_call: ToolCall,
        result: AgentToolResult,
        is_error: bool,
    ) -> tuple[AgentToolResult, bool]:
        """执行 after_tool_call hook 链（工具执行后）

        按注册顺序执行 matcher 命中的 hook；hook 可返回 (result, is_error) 改写结果。
        fail-closed：hook 异常时默认返回错误结果。
        """
        from dot.ai.types import TextContent

        for entry in list(self._hooks["after_tool_call"]):
            if not entry.matcher.search(tool_call.name or ""):
                continue
            try:
                modified = entry.fn(tool_call, result, is_error)
                if isawaitable(modified):
                    modified = await modified
                if isinstance(modified, tuple) and len(modified) == 2:
                    result, is_error = modified
            except Exception as exc:
                logger.error(
                    "[extensions] after_tool_call hook '%s' error (fail-closed): %s",
                    entry.name, exc,
                )
                result = AgentToolResult(
                    content=[TextContent(text=f"Hook '{entry.name}' error: {exc}")],
                    details={},
                )
                is_error = True
                return result, is_error
        return result, is_error

    async def run_lifecycle_hooks(self, timing: str, event: AgentEvent) -> None:
        """执行生命周期 hook（agent_start / turn_end / tool_execution_end 等观察点）

        matcher 正则匹配时机名（默认 .* 全命中）；返回值忽略，异常只记日志。
        """
        # 工具事件按工具名匹配 matcher，其余生命周期时机按时机名匹配
        tool_name = getattr(event, "tool_name", None)
        subject = tool_name if tool_name is not None else timing
        for entry in list(self._hooks.get(timing, [])):
            if not entry.matcher.search(subject):
                continue
            try:
                result = entry.fn(event)
                if isawaitable(result):
                    await result
            except Exception as exc:
                logger.error("[extensions] %s hook '%s' error: %s", timing, entry.name, exc)

    # ============================================================
    # 与 AgentHarness 的事件流对接
    # ============================================================

    def attach_to_harness(self, harness: Any) -> None:
        """订阅 harness 的 AgentEvent 流，驱动生命周期 hook

        同一 runtime 可安全多次 attach（先解绑旧的）。
        """
        if getattr(self, "_harness_unsub", None) is not None:
            self._harness_unsub()
        runtime = self

        async def _on_agent_event(event: AgentEvent) -> None:
            timing = timing_for_event(event)
            if timing is not None:
                await runtime.run_lifecycle_hooks(timing, event)
            await runtime._notify_event_listeners(event, timing)


        self._harness_unsub = harness.subscribe(_on_agent_event)

    # ============================================================
    # 事件分发
    # ============================================================

    async def _notify_event_listeners(self, event: AgentEvent, timing: str | None) -> None:
        """分发事件给 on_event 注册的被动监听（按 event_type 过滤）

        event_type 支持时机名（如 "tool_execution_end"）或 "*"/"all"（全收）；
        未知时机的事件只投递给通配监听。拷贝列表防止迭代中修改。
        """
        if timing is None and not any(
            sub.event_type in ("*", "all", "") for sub in self._event_listeners
        ):
            return
        for sub in list(self._event_listeners):
            if not self._listener_matches(sub, timing):
                continue
            try:
                result = sub.fn(event)
                if isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug("[extensions] Event listener error: %s", exc)

    @staticmethod
    def _listener_matches(sub: EventSubscription, timing: str | None) -> bool:
        et = sub.event_type
        if et in ("*", "all", ""):
            return True
        return timing is not None and et == timing

    # ============================================================
    # 运行时依赖
    # ============================================================

    def set_runtime_dep(self, key: str, value: Any) -> None:
        self._runtime_deps[key] = value

    def get_runtime_dep(self, key: str, default: Any = None) -> Any:
        return self._runtime_deps.get(key, default)

    def make_tool_hooks(self) -> tuple[object, object]:
        """生成适配 agent loop 的 before_tool_call / after_tool_call 回调

        返回的回调直接绑定到 run_agent_loop 的 before_tool_call / after_tool_call 参数，
        实现 fail-closed hook 链：
          tool_call hook（执行前，可阻止） → 原始工具执行 → tool_result hook（执行后，可修改结果）
        """
        runtime = self

        async def before_tool_call(call: ToolCall) -> tuple[bool, str | None]:
            return await runtime.run_tool_call_hooks(call)

        async def after_tool_call(
            call: ToolCall,
            result: AgentToolResult,
            is_error: bool,
        ) -> tuple[AgentToolResult, bool]:
            return await runtime.run_tool_result_hooks(call, result, is_error)

        return before_tool_call, after_tool_call


# ============================================================
# _RuntimeAPI — ExtensionAPI 的运行时实现
# ============================================================

class _RuntimeAPI(ExtensionAPI):
    """ExtensionAPI 的运行时实现

    扩展通过此对象注册工具、命令、事件监听。
    """

    def __init__(self, runtime: ExtensionRuntime, generation: ExtensionGeneration) -> None:
        self._runtime = runtime
        self._generation = generation

    def _check_alive(self) -> None:
        self._generation.ensure_alive()

    def register_tool(self, tool: AgentTool) -> None:
        self._check_alive()
        self._runtime._tools[tool.name] = tool

    def register_command(self, name: str, handler: Callable[..., Any] | None = None) -> Any:
        """注册斜杠命令，支持两种调用方式：
        ext.register_command("name", handler)   # 直接调用
        @ext.register_command("name")           # 装饰器
        def handler(...): ...
        """
        self._check_alive()
        if handler is not None:
            self._runtime._commands[name] = handler
            return handler

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._runtime._commands[name] = fn
            return fn
        return decorator

    def on_event(
        self,
        event_type: str,
        handler: EventListener | None = None,
    ) -> Any:
        """订阅事件，支持装饰器用法"""
        self._check_alive()
        if handler is not None:
            self._runtime._event_listeners.append(
                EventSubscription(event_type=event_type, fn=handler),
            )
            return handler

        def decorator(fn: EventListener) -> EventListener:
            self._runtime._event_listeners.append(
                EventSubscription(event_type=event_type, fn=fn),
            )
            return fn
        return decorator

    def add_prompt_section(self, section: str) -> None:
        self._check_alive()
        self._runtime._prompt_sections.append(section)

    def register_hook(
        self,
        timing: str,
        fn: HookFn | None = None,
        *,
        name: str = "",
        matcher: str = ".*",
    ) -> Any:
        """注册任意时机的 hook（支持装饰器用法）

        timing: HOOK_TIMINGS 之一（before_tool_call / after_tool_call / agent_start /
                agent_end / turn_start / turn_end / message_start / message_end /
                tool_execution_start / tool_execution_update / tool_execution_end）
        matcher: 正则，对工具时机匹配工具名、对生命周期时机匹配时机名；默认全命中
        """
        self._check_alive()
        if timing not in HOOK_TIMINGS:
            raise ValueError(
                f"Unknown hook timing: {timing!r} (valid: {', '.join(HOOK_TIMINGS)})"
            )
        compiled = re.compile(matcher)

        def _register(f: HookFn) -> HookFn:
            self._runtime._hooks[timing].append(HookEntry(
                name=name or getattr(f, "__name__", "hook"),
                timing=timing, fn=f, matcher=compiled,
            ))
            return f

        if fn is not None:
            return _register(fn)
        return _register

    def register_tool_call_hook(self, name: str, fn: HookFn | None = None) -> Any:
        """兼容旧 API：等价于 register_hook("before_tool_call", ...)"""
        return self.register_hook("before_tool_call", fn, name=name)

    def register_tool_result_hook(self, name: str, fn: HookFn | None = None) -> Any:
        """兼容旧 API：等价于 register_hook("after_tool_call", ...)"""
        return self.register_hook("after_tool_call", fn, name=name)

    def send_user_message(
        self,
        content: str,
        *,
        deliver_as: str = "follow_up",
    ) -> None:
        """把消息排队到当前运行中的 harness。"""
        self._check_alive()
        host = self._runtime._runtime_deps.get("host")
        harness = getattr(host, "_harness", None) if host is not None else None
        if harness is None:
            logger.warning("[extensions] send_user_message: no harness, dropped: %s", content[:80])
            return
        if deliver_as == "steer":
            harness.steer(content)
        elif deliver_as == "follow_up":
            harness.follow_up(content)
        else:
            raise ValueError("deliver_as must be 'steer' or 'follow_up'")
        logger.info("[extensions] send_user_message queued as %s: %s", deliver_as, content[:80])

    def queue_steering_message(self, content: str) -> None:
        self.send_user_message(content, deliver_as="steer")

    def queue_follow_up_message(self, content: str) -> None:
        self.send_user_message(content, deliver_as="follow_up")
