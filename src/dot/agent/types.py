"""
dot.agent.types — Agent Loop 共享类型

TokenUsage 等 Agent Loop 共享类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dot.ai.types import AssistantMessage, ToolCall


@dataclass
class TokenUsage:
    """Token 用量统计"""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0


@dataclass
class ToolCallRecord:
    """工具调用记录"""
    tool_call_id: str
    tool_name: str
    arguments: dict
    is_error: bool = False
    result_summary: str = ""



