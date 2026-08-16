"""
Hook 系统 — Agent 生命周期确定性拦截

在工具执行的各个生命周期节点插入外部逻辑。
与提示词（概率性约束）不同，Hook 是应用层强制执行，一定会跑。

事件类型：
- PreToolUse: 工具执行前，可阻断 / 修改参数
- PostToolUse: 工具执行成功后
- PostToolUseFailure: 工具执行失败后
- SessionStart: 会话启动
- SessionEnd: 会话结束
- UserPromptSubmit: 用户提交输入后（可选）

用法：
    runner = HookRunner()
    runner.register(Hook(
        name="block-dangerous-bash",
        events=(HookEvent.PreToolUse,),
        matcher=r"^Bash",
        handler=my_handler,
    ))
    result = runner.run(HookEvent.PreToolUse, payload)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class HookEvent(Enum):
    """Hook 生命周期事件"""
    PreToolUse = "PreToolUse"
    PostToolUse = "PostToolUse"
    PostToolUseFailure = "PostToolUseFailure"
    SessionStart = "SessionStart"
    SessionEnd = "SessionEnd"
    UserPromptSubmit = "UserPromptSubmit"
    UserPromptExpansion = "UserPromptExpansion"
    # 压缩前触发（对齐 Claude Code PreCompact；matcher 区分 manual/auto）
    PreCompact = "PreCompact"
    # 模型 end_turn / 子代理结束（对齐 Claude Code Stop / SubagentStop）
    Stop = "Stop"
    SubagentStop = "SubagentStop"
    StopFailure = "StopFailure"


# 工具相关事件（需要 matcher 过滤）
_TOOL_EVENTS = frozenset({
    HookEvent.PreToolUse,
    HookEvent.PostToolUse,
    HookEvent.PostToolUseFailure,
})


@dataclass
class HookPayload:
    """传递给 Hook handler 的上下文数据"""
    event: HookEvent
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    error: Exception | None = None
    session_id: str = ""
    workspace: str = ""
    user_prompt: str = ""
    # PreCompact: manual | auto
    compact_trigger: str = ""
    # SessionStart hook 的 stdout 会作为 additional_context 注入
    additional_context: str = ""


@dataclass
class HookResult:
    """Hook handler 的返回值"""
    # 是否阻断本次操作（仅 PreToolUse / UserPromptSubmit 有效）
    blocked: bool = False
    # 阻断时的反馈信息，会作为 tool_result 返回给模型
    feedback: str = ""
    # 修改后的工具参数（仅 PreToolUse 有效）
    updated_args: dict[str, Any] | None = None
    # SessionStart / UserPromptSubmit 等事件注入到上下文的文本（对齐 Claude Code stdout→context）
    context_injection: str = ""
    # PreToolUse: allow | deny | ask（对齐 Claude Code permissionDecision）
    permission_decision: str = ""
    # Stop hook 可要求继续（preventContinuation=false 时 force continue）
    prevent_continuation: bool = False


@dataclass
class Hook:
    """单个 Hook 定义"""
    name: str
    # 处理函数
    handler: Callable[[HookPayload], HookResult]
    # 监听的事件；空表示兼容旧行为（仅工具事件）
    events: tuple[HookEvent, ...] = ()
    # 正则表达式，匹配工具名。空字符串表示匹配所有工具事件。
    matcher: str = ""
    # 优先级，数字越小越先执行
    priority: int = 100
    # 编译后的正则（由 HookRunner 编译）
    _compiled_pattern: re.Pattern[str] | None = field(default=None, repr=False)

    def listens_to(self, event: HookEvent) -> bool:
        """是否监听指定事件"""
        if self.events:
            return event in self.events
        # 未声明 events 时，仅绑定工具事件（避免 SessionStart 误触 Bash hook）
        return event in _TOOL_EVENTS

    def matches(self, tool_name: str) -> bool:
        """检查工具名是否匹配"""
        if not self.matcher:
            return True
        if self._compiled_pattern is None:
            self._compiled_pattern = re.compile(self.matcher)
        return bool(self._compiled_pattern.search(tool_name))


class HookRunner:
    """Hook 执行引擎

    管理所有注册的 Hook，在生命周期事件触发时按优先级执行。
    """

    def __init__(self) -> None:
        self._hooks: list[Hook] = []

    def register(self, hook: Hook) -> None:
        """注册一个 Hook"""
        self._hooks.append(hook)
        self._hooks.sort(key=lambda h: h.priority)

    def unregister(self, name: str) -> bool:
        """按名字注销 Hook"""
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        return len(self._hooks) < before

    def clear(self) -> None:
        """清除所有 Hook"""
        self._hooks.clear()

    @property
    def hook_count(self) -> int:
        return len(self._hooks)

    def run(self, event: HookEvent, payload: HookPayload) -> HookResult:
        """执行匹配的 Hook，返回合并后的结果

        执行规则：
        - 按 priority 排序执行
        - 任意一个 Hook 返回 blocked=True 则立即阻断
        - 多个 Hook 可修改参数，最后一个生效
        - 单个 Hook 异常不影响其他 Hook 执行
        """
        merged = HookResult()
        payload.event = event

        for hook in self._hooks:
            if not hook.listens_to(event):
                continue

            # 工具事件做 matcher 过滤；会话事件忽略 matcher
            if event in _TOOL_EVENTS and payload.tool_name:
                if not hook.matches(payload.tool_name):
                    continue

            try:
                result = hook.handler(payload)
            except Exception as exc:
                logger.warning("Hook '%s' raised %s: %s", hook.name, type(exc).__name__, exc)
                continue

            if result.blocked or result.permission_decision == "deny":
                logger.info("Hook '%s' blocked tool '%s'", hook.name, payload.tool_name)
                merged.blocked = True
                merged.feedback = result.feedback or result.permission_decision
                merged.permission_decision = "deny"
                return merged

            if result.permission_decision == "ask":
                merged.permission_decision = "ask"
                if result.feedback:
                    merged.feedback = result.feedback

            if result.updated_args is not None:
                merged.updated_args = result.updated_args

            if result.context_injection:
                merged.context_injection = (
                    f"{merged.context_injection}\n{result.context_injection}"
                    if merged.context_injection
                    else result.context_injection
                )

            if result.prevent_continuation:
                merged.prevent_continuation = True

        return merged


def fire_session_hook(
    runner: HookRunner | None,
    event: HookEvent,
    *,
    workspace: str = "",
    session_id: str = "",
) -> HookResult:
    """便捷触发会话级 Hook"""
    if runner is None:
        return HookResult()
    return runner.run(
        event,
        HookPayload(event=event, workspace=workspace, session_id=session_id),
    )


def fire_stop_hook(
    runner: HookRunner | None,
    *,
    workspace: str = "",
    session_id: str = "",
    subagent: bool = False,
) -> HookResult:
    """触发 Stop / SubagentStop"""
    if runner is None:
        return HookResult()
    event = HookEvent.SubagentStop if subagent else HookEvent.Stop
    return runner.run(
        event,
        HookPayload(event=event, workspace=workspace, session_id=session_id),
    )
