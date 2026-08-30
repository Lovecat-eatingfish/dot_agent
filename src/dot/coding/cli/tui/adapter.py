"""
dot.coding.cli.tui.adapter — TuiEventAdapter

将 AgentEvent 转换为 TuiState 更新。
参考 Tau 的 TuiEventAdapter 设计。
"""
from __future__ import annotations

from dot.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from dot.ai.events import TextDeltaEvent, ThinkingDeltaEvent
from dot.ai.types import AssistantMessage, ToolCall, UserMessage

from .state import TuiState


class TuiEventAdapter:
    """将 AgentEvent 转换为 TuiState 更新"""

    def __init__(self, state: TuiState) -> None:
        self.state = state
        self._assistant_start_item_index: int | None = None

    def apply(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self.state.running = True
            self.state.error = None
            return

        if isinstance(event, AgentEndEvent):
            self._flush()
            self.state.running = False
            return

        if isinstance(event, TurnStartEvent):
            return

        if isinstance(event, TurnEndEvent):
            return

        if isinstance(event, MessageStartEvent):
            msg = event.message
            if isinstance(msg, AssistantMessage):
                self.state.assistant_buffer = msg.text
                self._assistant_start_item_index = len(self.state.items)
            return

        if isinstance(event, MessageUpdateEvent):
            nested = event.provider_event
            if isinstance(nested, TextDeltaEvent):
                self.state.assistant_buffer += nested.delta
            elif isinstance(nested, ThinkingDeltaEvent):
                if self.state.show_thinking:
                    self._append_thinking(nested.delta)
            return

        if isinstance(event, MessageEndEvent):
            msg = event.message
            if isinstance(msg, UserMessage):
                self.state.add_user_message(msg.text)
            elif isinstance(msg, AssistantMessage):
                # 用最终消息替换流式累积的临时行
                start = self._assistant_start_item_index
                if start is not None:
                    del self.state.items[start:]
                if msg.stop_reason in {"error", "aborted"}:
                    self.state.add_assistant_error(msg)
                else:
                    self.state.add_assistant_message(msg)
                self.state.assistant_buffer = ""
                self._assistant_start_item_index = None
            return

        if isinstance(event, ToolExecutionStartEvent):
            self._flush()
            tool_call = ToolCall(
                id=event.tool_call_id,
                name=event.tool_name,
                arguments=event.args,
            )
            self.state.add_tool_call(tool_call)
            return

        if isinstance(event, ToolExecutionUpdateEvent):
            self.state.record_tool_update(event.tool_call_id, event.partial_result.text)
            return

        if isinstance(event, ToolExecutionEndEvent):
            self.state.record_tool_result(
                event.tool_call_id,
                event.tool_name,
                event.result.text,
                event.is_error,
            )
            return

    def _flush(self) -> None:
        if self.state.assistant_buffer:
            self.state.add_item("assistant", self.state.assistant_buffer)
            self.state.assistant_buffer = ""

    def _append_thinking(self, delta: str) -> None:
        if self.state.items and self.state.items[-1].role == "thinking":
            self.state.items[-1].text += delta
        else:
            self.state.add_item("thinking", delta)
