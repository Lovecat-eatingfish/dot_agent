"""
dot.coding.extensions.runtime — ExtensionRuntime

管理扩展生命周期、事件分发、fail-closed hook 链。

关键设计（来自 Tau 的洞察）：
- ExtensionGeneration liveness token：每次 /reload 创建新 generation，旧引用立即失效
- fail-closed hook 链：hook 异常时默认阻止工具执行（block=True）
- _notify 拷贝监听器列表：防止回调中取消订阅时的"迭代中修改集合"异常
- accepting 标志：工具执行完毕后设为 False，防止幽灵更新
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable, signature
from pathlib import Path
from typing import Any

from dot.agent.events import AgentEvent
from dot.agent.tools import AgentTool, AgentToolResult
from dot.ai.types import ToolCall

from .api import ExtensionContext
from .generation import ExtensionError, ExtensionGeneration
from .loader import ExtensionLoader, ExtensionModule

logger = logging.getLogger(__name__)

HookFn = Callable[..., Awaitable[None] | None]
EventListener = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass
class HookEntry:
    """一个注册的 hook"""
    name: str
    fn: HookFn
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

        # Hook 链：tool_call hook（执行前，可阻止/重写参数）
        self._tool_call_hooks: list[HookEntry] = []
        # Hook 链：tool_result hook（执行后，可修改结果）
        self._tool_result_hooks: list[HookEntry] = []

        # 事件监听
        self._event_listeners: list[EventSubscription] = []

        # 运行时依赖（BuiltInExtensionContext / ExtensionContext 使用）
        # 用来存放运行时注入的依赖。外部代码可以通过 set_runtime_dep(key, value) 往里面塞东西。
        # 注入的时 AgentHost对象
        self._runtime_deps: dict[str, Any] = {}

        # 已加载模块及其上下文（reload 时用于 teardown）
        self._loaded: list[tuple[ExtensionModule, ExtensionContext]] = []

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
        self._tool_call_hooks.clear()
        self._tool_result_hooks.clear()
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
        """执行 tool_call hook 链（工具执行前）

        Returns:
            (blocked, reason) — blocked=True 表示阻止执行

        fail-closed：hook 异常时默认阻止工具执行。
        """
        for entry in list(self._tool_call_hooks):
            try:
                result = entry.fn(tool_call)
                if isawaitable(result):
                    result = await result
                if isinstance(result, tuple) and len(result) == 2:
                    blocked, reason = result
                    if blocked:
                        return True, reason or f"Blocked by hook '{entry.name}'"
            except Exception as exc:
                logger.error(
                    "[extensions] tool_call hook '%s' error (fail-closed): %s",
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
        """执行 tool_result hook 链（工具执行后）

        Returns:
            (result, is_error) — 可能被 hook 修改

        fail-closed：hook 异常时默认返回错误结果。
        """
        from dot.ai.types import TextContent

        for entry in list(self._tool_result_hooks):
            try:
                modified = entry.fn(tool_call, result, is_error)
                if isawaitable(modified):
                    modified = await modified
                if isinstance(modified, tuple) and len(modified) == 2:
                    result, is_error = modified
            except Exception as exc:
                logger.error(
                    "[extensions] tool_result hook '%s' error (fail-closed): %s",
                    entry.name, exc,
                )
                result = AgentToolResult(
                    content=[TextContent(text=f"Hook '{entry.name}' error: {exc}")],
                    details={},
                )
                is_error = True
                return result, is_error
        return result, is_error

    # ============================================================
    # 事件分发
    # ============================================================

    async def dispatch_event(self, event: AgentEvent) -> None:
        """分发事件给订阅者

        拷贝监听器列表，防止回调中取消订阅时的"迭代中修改集合"异常。
        """
        for sub in list(self._event_listeners):
            try:
                result = sub.fn(event)
                if isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug("[extensions] Event listener error: %s", exc)

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

class _RuntimeAPI:
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

    def register_tool_call_hook(self, name: str, fn: HookFn | None = None) -> Any:
        """注册 tool_call hook，支持装饰器用法"""
        self._check_alive()
        if fn is not None:
            self._runtime._tool_call_hooks.append(HookEntry(name=name, fn=fn))
            return fn

        def decorator(f: HookFn) -> HookFn:
            self._runtime._tool_call_hooks.append(HookEntry(name=name, fn=f))
            return f
        return decorator

    def register_tool_result_hook(self, name: str, fn: HookFn | None = None) -> Any:
        """注册 tool_result hook，支持装饰器用法"""
        self._check_alive()
        if fn is not None:
            self._runtime._tool_result_hooks.append(HookEntry(name=name, fn=fn))
            return fn

        def decorator(f: HookFn) -> HookFn:
            self._runtime._tool_result_hooks.append(HookEntry(name=name, fn=f))
            return f
        return decorator

    def send_user_message(self, content: str) -> None:
        self._check_alive()
        # TODO: 注入 follow-up 消息到 AgentHarness
        logger.info("[extensions] send_user_message queued: %s", content[:80])
