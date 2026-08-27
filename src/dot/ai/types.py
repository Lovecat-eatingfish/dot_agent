"""
dot.ai.types — 共享类型定义

Provider 层的基础类型：消息、内容块、工具调用等。
所有类型使用 Pydantic BaseModel，支持 JSON 序列化。
"""
from __future__ import annotations

from time import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ============================================================
# 基础配置
# ============================================================

def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


class WireModel(BaseModel):
    """严格模型：Python 字段名 + camelCase JSON 别名"""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        alias_generator=_to_camel,
    )


# ============================================================
# JSON 值类型
# ============================================================

JSONValue = str | int | float | bool | None | dict[str, Any] | list[Any]


# ============================================================
# Usage / Timing
# ============================================================

class UsageCost(WireModel):
    """响应费用（USD）"""
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class Usage(WireModel):
    """Provider 报告的 token 用量"""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = UsageCost()


class ResponseTiming(WireModel):
    """请求耗时"""
    time_to_first_output_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int = Field(ge=0)


# ============================================================
# 内容块
# ============================================================

class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingContent(WireModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    redacted: bool = False


class ImageContent(WireModel):
    type: Literal["image"] = "image"
    data: str
    mime_type: str


class ToolCall(WireModel):
    """Assistant 请求的工具调用"""
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)


# ============================================================
# 消息类型
# ============================================================

def _current_timestamp_ms() -> int:
    return int(time() * 1000)


UserContent = str | list[TextContent | ImageContent]
AssistantContent = TextContent | ThinkingContent | ToolCall
ToolResultContent = TextContent | ImageContent


class UserMessage(WireModel):
    role: Literal["user"] = "user"
    content: UserContent
    timestamp: int = Field(default_factory=_current_timestamp_ms)

    @property
    def text(self) -> str:
        return content_text(self.content)


StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


class AssistantMessage(WireModel):
    """Assistant 消息，包含有序内容块"""
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    model: str = "unknown"
    provider: str = "unknown"
    usage: Usage = Usage()
    timing: ResponseTiming | None = None
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=_current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        if data.get("usage") is None:
            data["usage"] = Usage()
        return data

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextContent))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(b for b in self.content if isinstance(b, ToolCall))


class ToolResultMessage(WireModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent] = Field(default_factory=list)
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    is_error: bool = False
    timestamp: int = Field(default_factory=_current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return content_text(self.content)


class SystemMessage(WireModel):
    role: Literal["system"] = "system"
    content: str
    timestamp: int = Field(default_factory=_current_timestamp_ms)


type AgentMessage = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | SystemMessage,
    Field(discriminator="role"),
]


# ============================================================
# 工具函数
# ============================================================

def content_text(content: str | list[Any]) -> str:
    """从内容块提取可见文本"""
    if isinstance(content, str):
        return content
    return "".join(b.text for b in content if isinstance(b, TextContent))


def assistant_content(
    text: str,
    tool_calls: list[ToolCall] | tuple[ToolCall, ...] = (),
) -> list[AssistantContent]:
    """从文本和工具调用构建规范的内容块列表"""
    blocks: list[AssistantContent] = [TextContent(text=text)] if text else []
    blocks.extend(tool_calls)
    return blocks
