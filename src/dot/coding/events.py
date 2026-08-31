"""
dot.coding.events — CodingEvent discriminated union

Coding 会话层事件，描述编码会话级别的状态变化。
订阅这些事件的包括：TUI、TraceCollector、扩展。

三层事件流：
  ProviderEvent (LLM 流式) → AgentEvent (Agent 生命周期) → CodingEvent (编码会话)
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dot.ai.types import WireModel


class CompactionEvent(WireModel):
    """上下文压缩触发"""
    type: Literal["compaction"] = "compaction"
    level: Literal["l1", "l2", "l3"]
    before_tokens: int = 0
    after_tokens: int = 0


class SessionInfoChangedEvent(WireModel):
    """会话信息变更（标题、标签等）"""
    type: Literal["session_info_changed"] = "session_info_changed"
    session_id: str
    field_name: str
    old_value: str = ""
    new_value: str = ""


class ThinkingLevelChangedEvent(WireModel):
    """思考级别变更"""
    type: Literal["thinking_level_changed"] = "thinking_level_changed"
    level: str  # "off" | "low" | "medium" | "high"


class ExtensionLoadedEvent(WireModel):
    """扩展加载/重载"""
    type: Literal["extension_loaded"] = "extension_loaded"
    extension_name: str
    generation_id: int = 0


class ModeChangedEvent(WireModel):
    """Agent 模式变更"""
    type: Literal["mode_changed"] = "mode_changed"
    mode: str  # "plan" | "edit" | "auto"
    old_mode: str = ""


type CodingEvent = Annotated[
    CompactionEvent
    | SessionInfoChangedEvent
    | ThinkingLevelChangedEvent
    | ExtensionLoadedEvent
    | ModeChangedEvent,
    Field(discriminator="type"),
]
