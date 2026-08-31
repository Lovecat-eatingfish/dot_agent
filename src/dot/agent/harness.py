"""
dot.agent.harness — AgentHarness 状态管理器

管理消息历史、事件订阅、取消令牌、双队列消息（steering + follow-up）。
事件通知时拷贝监听器列表，防止迭代中修改集合。
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import TYPE_CHECKING, Literal

from dot.ai.types import AgentMessage, AssistantMessage, TextContent, ToolResultMessage, UserMessage

from .cancel import SimpleCancellationToken
from .events import AgentEvent, MessageEndEvent, MessageStartEvent
from .loop import AfterToolCall, BeforeToolCall, run_agent_loop
from .tools import AgentTool
from ..ai.providers import OpenAIProvider

if TYPE_CHECKING:
    # 仅类型标注使用：运行时不导入，避免 agent → coding 的循环导入
    # （权限管理器实例由 CodingHost 通过 AgentHarnessConfig 注入）
    from ..coding.permission import PermissionManager

EventListener = Callable[[AgentEvent], Awaitable[None] | None]
QueueMode = Literal["one_at_a_time", "all"]


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    steering: tuple[AgentMessage, ...] = ()
    follow_up: tuple[AgentMessage, ...] = ()

    @property
    def count(self) -> int:
        return len(self.steering) + len(self.follow_up)


@dataclass(slots=True)
class AgentHarnessConfig:
    provider: OpenAIProvider  # ModelProvider
    model: str
    system: str
    tools: list[AgentTool] = field(default_factory=list)
    max_turns: int | None = None
    queue_mode: QueueMode = "one_at_a_time"
    session_id: str | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    permission: PermissionManager | None = None
    agent_mode: str = "auto"


class AgentHarness:
    """可复用的有状态 Agent 核心，独立于 coding/UI 策略"""

    def __init__(
            self,
            config: AgentHarnessConfig,
            *,
            messages: Sequence[AgentMessage] = (),
    ) -> None:
        self._config = config
        self._messages = list(messages)
        self._listeners: list[EventListener] = []
        self._current_signal: SimpleCancellationToken | None = None
        self._running = False
        self._steering_queue: deque[AgentMessage] = deque()
        self._follow_up_queue: deque[AgentMessage] = deque()

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return tuple(self._messages)

    @property
    def config(self) -> AgentHarnessConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def queued_messages(self) -> QueuedMessages:
        return QueuedMessages(tuple(self._steering_queue), tuple(self._follow_up_queue))

    @property
    def pending_message_count(self) -> int:
        return self.queued_messages.count

    def has_queued_messages(self) -> bool:
        return bool(self._steering_queue or self._follow_up_queue)

    def append_message(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def replace_messages(self, messages: Sequence[AgentMessage]) -> None:
        self._messages = list(messages)

    def set_tools(self, tools: Sequence[AgentTool]) -> None:
        """热更新可用工具（扩展 /reload 后刷新当前 harness）"""
        self._config.tools = list(tools)

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """
        # 使用场景
        event_bus = EventBus()

        # 订阅事件
        def my_handler(event):
            print(f"处理: {event}")

        unsubscribe = event_bus.subscribe(my_handler)  # 获取取消订阅函数

        # 稍后取消订阅
        unsubscribe()  # 闭包记住了要移除哪个 listener
        :param listener:
        :return:
        """
        self._listeners.append(listener)  # 保存监听器

        def unsubscribe() -> None:  # 闭包函数
            with suppress(ValueError):
                self._listeners.remove(listener)  # 捕获外部变量 listener

        return unsubscribe  # 返回闭包

    def cancel(self) -> None:
        if self._current_signal is not None:
            self._current_signal.cancel()

    def steer(self, content: str) -> QueuedMessages:
        return self.steer_message(UserMessage(content=content))

    def steer_message(self, message: AgentMessage) -> QueuedMessages:
        self._steering_queue.append(message)
        return self.queued_messages

    def follow_up(self, content: str) -> QueuedMessages:
        return self.follow_up_message(UserMessage(content=content))

    def follow_up_message(self, message: AgentMessage) -> QueuedMessages:
        self._follow_up_queue.append(message)
        return self.queued_messages

    def clear_queues(self) -> QueuedMessages:
        snapshot = self.queued_messages
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        return snapshot

    def pop_latest_steering(self) -> AgentMessage | None:
        """Remove and return the most recently queued steering message."""
        return self._steering_queue.pop() if self._steering_queue else None

    def pop_latest_follow_up(self) -> AgentMessage | None:
        """Remove and return the most recently queued follow-up message."""
        return self._follow_up_queue.pop() if self._follow_up_queue else None

    def prompt_message(self, message: AgentMessage, *, system: str | None = None) -> AsyncIterator[AgentEvent]:
        self._ensure_not_running()
        self._running = True
        return self._run(prompts=(message,), system_override=system)

    def prompt(self, content: str, *, system: str | None = None) -> AsyncIterator[AgentEvent]:
        return self.prompt_message(UserMessage(content=content), system=system)

    def continue_(self) -> AsyncIterator[AgentEvent]:
        self._ensure_not_running()
        self._running = True
        return self._run()

    async def _run(self, *, prompts: Sequence[AgentMessage] = (), system_override: str | None = None) -> AsyncIterator[
        AgentEvent]:
        signal = SimpleCancellationToken()
        self._current_signal = signal
        try:
            repaired_from = len(self._messages)
            self._append_interrupted_tool_results()
            repairs = self._messages[repaired_from:]
            system = system_override if system_override is not None else self._config.system
            async for event in run_agent_loop(
                    provider=self._config.provider,
                    model=self._config.model,
                    system=system,
                    messages=self._messages,
                    prompts=prompts,
                    prelude_messages=repairs,
                    tools=self._config.tools,
                    max_turns=self._config.max_turns,
                    signal=signal,
                    session_id=self._config.session_id,
                    get_steering_messages=self._drain_steering_messages,
                    get_follow_up_messages=self._drain_follow_up_messages,
                    before_tool_call=self._config.before_tool_call,
                    after_tool_call=self._config.after_tool_call,
                    permission=self._config.permission,
                    agent_mode=self._config.agent_mode,
            ):
                await self._notify(event)
                yield event
        finally:
            # asyncio.shield 保护资源清理任务不被外层取消
            # 外层被取消时 cleanup task 不被取消，保证资源清理完整性
            await self._shielded_cleanup(signal)

    async def _shielded_cleanup(self, signal: SimpleCancellationToken) -> None:
        """受 shield 保护的清理逻辑"""

        async def _cleanup() -> None:
            if signal.is_cancelled():
                repaired_from = len(self._messages)
                self._append_interrupted_tool_results()
                for message in self._messages[repaired_from:]:
                    with suppress(Exception):
                        await self._notify(MessageStartEvent(message=message))
                        await self._notify(MessageEndEvent(message=message))
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

        try:
            await asyncio.shield(_cleanup())
        except asyncio.CancelledError:
            # 即使外层被取消，清理也已在 shield 中完成
            pass

    async def _notify(self, event: AgentEvent) -> None:
        # 分发给 harness 监听器（TraceCollector、扩展 hook/事件监听经 attach_to_harness 接入）
        for listener in list(self._listeners):
            result = listener(event)
            if isawaitable(result):
                await result

    def _ensure_not_running(self) -> None:
        if self._running:
            raise RuntimeError(
                "AgentHarness is already running; use steer() or follow_up() to queue messages."
            )

    def _drain_steering_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._steering_queue)

    def _drain_follow_up_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._follow_up_queue)

    def _drain_queue(self, queue: deque[AgentMessage]) -> tuple[AgentMessage, ...]:
        if not queue:
            return ()
        if self._config.queue_mode == "all":
            messages = tuple(queue)
            queue.clear()
            return messages
        return (queue.popleft(),)

    def _append_interrupted_tool_results(self) -> None:
        """为中断的工具调用合成错误结果"""
        returned_ids = {
            msg.tool_call_id
            for msg in self._messages
            if isinstance(msg, ToolResultMessage)
        }
        for msg in tuple(self._messages):
            if not isinstance(msg, AssistantMessage):
                continue
            for call in msg.tool_calls:
                if call.id in returned_ids:
                    continue
                returned_ids.add(call.id)
                self._messages.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=[TextContent(text="Tool call interrupted by user")],
                        is_error=True,
                    )
                )
