"""
dot.agent.loop — run_agent_loop（内层循环）

流式模型响应 → 提取工具调用 → 执行工具 → 追加结果 → 循环直到模型停止调用工具。
steering / follow-up 双队列消息注入。
精确计时：手动展开 __anext__ 循环，用 monotonic_ns() 只捕获 provider 响应等待时间。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from time import monotonic_ns

from dot.ai.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantStartEvent,
    ProviderEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from dot.ai.types import (
    AgentMessage,
    AssistantMessage,
    ResponseTiming,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from .cancel import SimpleCancellationToken

from .events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from .history import repair_tool_history
from .tools import AgentTool, AgentToolResult
from ..ai.providers import OpenAIProvider

if TYPE_CHECKING:
    # 仅类型标注使用：运行时实例由 CodingHost 注入；运行时的 Decision 在
    # _execute_tool_call 内延迟导入，避免 agent → coding 的循环导入
    from ..coding.permission import PermissionManager

BeforeToolCall = Callable[[ToolCall], Awaitable[tuple[bool, str | None]]]
AfterToolCall = Callable[
    [ToolCall, AgentToolResult, bool],
    Awaitable[tuple[AgentToolResult, bool]],
]


async def run_agent_loop(
    *,
    provider: OpenAIProvider,  # ModelProvider
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    prompts: Sequence[AgentMessage] = (),
    prelude_messages: Sequence[AgentMessage] = (),
    max_turns: int | None = None,
    signal: SimpleCancellationToken | None = None,  # CancellationToken
    session_id: str | None = None,
    get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    permission: PermissionManager | None = None,
    agent_mode: str = "auto",
) -> AsyncIterator[AgentEvent]:
    """运行 Provider/Tool 循环，发射 Agent 事件

    Args:
        provider: ModelProvider 实现
        model: 模型标识
        system: 系统提示词
        messages: 消息历史（会被修改）
        tools: 可用工具
        prompts: 本轮用户消息
        prelude_messages: 预置消息（如修复的 tool result）
        max_turns: 最大 turn 数
        signal: 取消令牌
        session_id: 会话 ID
        get_steering_messages: 获取 steering 消息回调
        get_follow_up_messages: 获取 follow-up 消息回调
        before_tool_call: 工具执行前 hook
        after_tool_call: 工具执行后 hook
        permission: 权限管理器（None 表示跳过权限检查）
        agent_mode: 当前 agent 模式字符串（plan / edit / auto）

    Yields:
        AgentEvent: Agent 生命周期事件
    """
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    yield AgentStartEvent()
    yield TurnStartEvent()
    for message in prelude_messages:
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    if max_turns is not None and max_turns < 1:
        error = _error_message(model, "max_turns must be at least 1")
        messages.append(error)
        new_messages.append(error)
        yield MessageStartEvent(message=error)
        yield MessageEndEvent(message=error)
        yield TurnEndEvent(message=error)
        yield AgentEndEvent(messages=new_messages)
        return

    tool_by_name = {tool.name: tool for tool in tools}
    turn = 1
    first_turn = True
    pending = tuple(get_steering_messages() if get_steering_messages else ())

    while True:
        has_more_tools = True
        while has_more_tools or pending:
            if not first_turn:
                yield TurnStartEvent()
            first_turn = False

            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            if max_turns is not None and turn > max_turns:
                error = _error_message(model, f"Agent stopped after max_turns={max_turns}")
                messages.append(error)
                new_messages.append(error)
                yield MessageStartEvent(message=error)
                yield MessageEndEvent(message=error)
                yield TurnEndEvent(message=error)
                yield AgentEndEvent(messages=new_messages)
                return

            # 消费 assistant 流式事件
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=_provider_context(messages),
                tools=tools,
                signal=signal,
                session_id=session_id,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message

            if assistant is None:
                assistant = _error_message(model, "Provider produced no assistant message")
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)

            messages.append(assistant)
            new_messages.append(assistant)
            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(messages=new_messages)
                return

            # 执行工具调用
            tool_results: list[ToolResultMessage] = []
            calls = list(assistant.tool_calls)
            has_more_tools = bool(calls)
            for call in calls:
                async for event in _execute_tool_call(
                    call,
                    tool_by_name,
                    signal,
                    before_tool_call,
                    after_tool_call,
                    permission,
                    agent_mode,
                ):
                    yield event
                    if isinstance(event, MessageEndEvent) and isinstance(
                        event.message, ToolResultMessage
                    ):
                        tool_results.append(event.message)
                        messages.append(event.message)
                        new_messages.append(event.message)

            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            turn += 1
            pending = tuple(get_steering_messages() if get_steering_messages else ())

        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(messages=new_messages)


def _provider_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """返回可重放的消息，同时保留失败消息用于诊断"""
    replayable = tuple(
        msg
        for msg in messages
        if not (
            isinstance(msg, AssistantMessage)
            and msg.stop_reason in {"error", "aborted"}
            and not msg.content
        )
    )
    return list(repair_tool_history(replayable).messages)


async def _assistant_events(
    *,
    provider: OpenAIProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: SimpleCancellationToken | None,
    session_id: str | None,
) -> AsyncIterator[AgentEvent]:
    """消费 Provider 流式事件，转换为 Agent 事件"""
    source: AsyncIterator[ProviderEvent] = provider.stream_response(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        signal=signal,
        session_id=session_id,
    )
    started = False
    provider_elapsed_ns = 0
    first_output_elapsed_ns: int | None = None
    source_iterator = source.__aiter__()
    while True:
        wait_started_ns = monotonic_ns()
        try:
            event = await anext(source_iterator)
        except StopAsyncIteration:
            break
        provider_elapsed_ns += max(0, monotonic_ns() - wait_started_ns)
        if first_output_elapsed_ns is None and isinstance(
            event,
            (TextDeltaEvent, ThinkingDeltaEvent, ToolCallStartEvent,
             ToolCallDeltaEvent, ToolCallEndEvent),
        ):
            first_output_elapsed_ns = provider_elapsed_ns
        if isinstance(event, AssistantStartEvent):
            started = True
            yield MessageStartEvent(message=event.partial)
        elif isinstance(event, AssistantDoneEvent):
            event.message.timing = _response_timing(first_output_elapsed_ns, provider_elapsed_ns)
            if not started:
                yield MessageStartEvent(message=event.message)
            yield MessageEndEvent(message=event.message)
        elif isinstance(event, AssistantErrorEvent):
            event.error.timing = _response_timing(first_output_elapsed_ns, provider_elapsed_ns)
            if not started:
                yield MessageStartEvent(message=event.error)
            yield MessageEndEvent(message=event.error)
        else:
            yield MessageUpdateEvent(message=event.partial, provider_event=event)


def _response_timing(
    first_output_elapsed_ns: int | None,
    total_elapsed_ns: int,
) -> ResponseTiming:
    return ResponseTiming(
        time_to_first_output_ms=(
            first_output_elapsed_ns // 1_000_000 if first_output_elapsed_ns is not None else None
        ),
        total_duration_ms=total_elapsed_ns // 1_000_000,
    )


async def _execute_tool_call(
    call: ToolCall,
    tools: Mapping[str, AgentTool],
    signal: SimpleCancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
    permission: PermissionManager | None,
    agent_mode: str = "auto",
) -> AsyncIterator[AgentEvent]:
    """执行单个工具调用，发射工具执行事件

    执行顺序：extension before_hook → 权限检查（系统/项目/模式三层）→ 工具执行
    """
    from ..coding.permission import Decision  # 延迟导入避免循环依赖

    yield ToolExecutionStartEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        args=call.arguments,
    )

    blocked = False
    block_reason: str | None = None

    # 1. Extension before_hook（用户扩展优先拦截）
    if before_tool_call is not None:
        blocked, block_reason = await before_tool_call(call)

    # 2. 权限检查（三层拦截：系统黑名单 → 项目黑名单 → 模式规则）
    if not blocked and permission is not None:
        decision = permission.check(call.name, dict(call.arguments), agent_mode=agent_mode)
        if decision.decision is Decision.DENY:
            blocked = True
            block_reason = decision.deny_message()
        elif decision.decision is Decision.ASK:
            approved = await permission.ask_user(
                call.name, dict(call.arguments), decision, agent_mode=agent_mode,
            )
            if not approved:
                blocked = True
                block_reason = decision.deny_message()
            else:
                # 用户批准，重新检查（仅 bypass 模式规则，系统/项目黑名单仍生效）
                final_decision = permission.check(
                    call.name, dict(call.arguments), agent_mode=agent_mode, approved=True,
                )
                if final_decision.decision is Decision.DENY:
                    blocked = True
                    block_reason = final_decision.deny_message()

    if blocked:
        result = _error_result(block_reason or "Tool execution was blocked")
        is_error = True
    elif signal is not None and signal.is_cancelled():
        result = _error_result("Operation aborted")
        is_error = True
    else:
        tool = tools.get(call.name)
        if tool is None:
            result = _error_result(f"Tool {call.name} not found")
            is_error = True
        else:
            result, is_error, updates = await _run_tool(tool, call, signal)
            for update in updates:
                yield ToolExecutionUpdateEvent(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    partial_result=update,
                )

    if after_tool_call is not None:
        result, is_error = await after_tool_call(call, result, is_error)

    yield ToolExecutionEndEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        result=result,
        is_error=is_error,
    )
    message = ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=result.content,
        details=result.details,
        added_tool_names=result.added_tool_names,
        is_error=is_error,
    )
    yield MessageStartEvent(message=message)
    yield MessageEndEvent(message=message)


async def _run_tool(
    tool: AgentTool,
    call: ToolCall,
    signal: object | None,
) -> tuple[AgentToolResult, bool, list[AgentToolResult]]:
    updates: list[AgentToolResult] = []
    accepting = True

    def on_update(partial: AgentToolResult) -> None:
        if accepting:
            updates.append(partial.model_copy(deep=True))

    try:
        result = await tool.execute(call.id, call.arguments, signal, on_update)
        return result, False, updates
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _error_result(str(exc)), True, updates
    finally:
        accepting = False


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})


def _error_message(model: str, message: str) -> AssistantMessage:
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason="error",
        error_message=message,
    )
