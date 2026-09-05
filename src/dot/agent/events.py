"""
dot.agent.events — AgentEvent discriminated union

Agent 生命周期事件，前端和扩展订阅这些事件。
事件必须携带 model_copy(deep=True) 的快照，防止引用别名 bug。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dot.ai.events import ProviderEvent
from dot.ai.types import AgentMessage, AssistantMessage, ToolResultMessage, WireModel

from .tools import AgentToolResult

JSONValue = str | int | float | bool | None | dict | list


class AgentStartEvent(WireModel):
    """Agent 启动"""
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(WireModel):
    """Agent 结束"""
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage] = []


class TurnStartEvent(WireModel):
    """Turn 开始"""
    type: Literal["turn_start"] = "turn_start"


class TurnEndEvent(WireModel):
    """Turn 结束"""
    type: Literal["turn_end"] = "turn_end"
    message: AgentMessage
    tool_results: list[ToolResultMessage] = []


class MessageStartEvent(WireModel):
    """消息开始"""
    type: Literal["message_start"] = "message_start"
    message: AgentMessage


class MessageUpdateEvent(WireModel):
    """消息增量更新（逐 token）"""
    type: Literal["message_update"] = "message_update"
    message: AgentMessage
    provider_event: ProviderEvent = Field(serialization_alias="providerEvent")


class MessageEndEvent(WireModel):
    """消息结束"""
    type: Literal["message_end"] = "message_end"
    message: AgentMessage


class ToolExecutionStartEvent(WireModel):
    """工具执行开始"""
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = {}


class ToolExecutionUpdateEvent(WireModel):
    """工具执行增量更新"""
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = {}
    partial_result: AgentToolResult


class ToolExecutionEndEvent(WireModel):
    """工具执行结束"""
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool


class ContextCompactedEvent(WireModel):
    """上下文已自动压缩（turn 边界触发）"""
    type: Literal["context_compacted"] = "context_compacted"
    level: str = ""    # 已应用的级别，如 "L1+L2"
    before: int = 0    # 压缩前消息数
    after: int = 0     # 压缩后消息数
    reason: str = ""   # 触发原因


type AgentEvent = Annotated[
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | ContextCompactedEvent,
    Field(discriminator="type"),
]
