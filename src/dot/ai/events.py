"""
dot.ai.events — ProviderEvent discriminated union

Provider 层的流式事件，由 ModelProvider.stream_response 产出。
前端和 Agent Loop 消费这些事件进行渲染或构建消息。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .types import AssistantMessage, ToolCall, WireModel


# ============================================================
# 流式事件
# ============================================================

class AssistantStartEvent(WireModel):
    """Assistant 响应开始"""
    type: Literal["start"] = "start"
    partial: AssistantMessage


class TextStartEvent(WireModel):
    """文本内容块开始"""
    type: Literal["text_start"] = "text_start"
    content_index: int
    partial: AssistantMessage


class TextDeltaEvent(WireModel):
    """文本增量"""
    type: Literal["text_delta"] = "text_delta"
    content_index: int
    delta: str
    partial: AssistantMessage


class TextEndEvent(WireModel):
    """文本内容块结束"""
    type: Literal["text_end"] = "text_end"
    content_index: int
    content: str
    partial: AssistantMessage


class ThinkingStartEvent(WireModel):
    """思考内容块开始"""
    type: Literal["thinking_start"] = "thinking_start"
    content_index: int
    partial: AssistantMessage


class ThinkingDeltaEvent(WireModel):
    """思考增量"""
    type: Literal["thinking_delta"] = "thinking_delta"
    content_index: int
    delta: str
    partial: AssistantMessage


class ThinkingEndEvent(WireModel):
    """思考内容块结束"""
    type: Literal["thinking_end"] = "thinking_end"
    content_index: int
    content: str
    partial: AssistantMessage


class ToolCallStartEvent(WireModel):
    """工具调用开始"""
    type: Literal["toolcall_start"] = "toolcall_start"
    content_index: int
    partial: AssistantMessage


class ToolCallDeltaEvent(WireModel):
    """工具调用参数增量"""
    type: Literal["toolcall_delta"] = "toolcall_delta"
    content_index: int
    delta: str
    partial: AssistantMessage


class ToolCallEndEvent(WireModel):
    """工具调用完成"""
    type: Literal["toolcall_end"] = "toolcall_end"
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage


# ============================================================
# 终结事件
# ============================================================

DoneReason = Literal["stop", "length", "toolUse"]
ErrorReason = Literal["aborted", "error"]


class AssistantDoneEvent(WireModel):
    """Assistant 响应正常结束"""
    type: Literal["done"] = "done"
    reason: DoneReason
    message: AssistantMessage


class AssistantErrorEvent(WireModel):
    """Assistant 响应异常结束"""
    type: Literal["error"] = "error"
    reason: ErrorReason
    error: AssistantMessage


# ============================================================
# Discriminated Union
# ============================================================

type ProviderEvent = Annotated[
    AssistantStartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | AssistantDoneEvent
    | AssistantErrorEvent,
    Field(discriminator="type"),
]
