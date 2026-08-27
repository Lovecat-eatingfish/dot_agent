"""
dot.agent.types — Agent Loop 共享类型

AgentLoopResult / AgentLoopConfig / TokenUsage 等。
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


@dataclass
class AgentLoopResult:
    """Inner Loop 返回值

    通过函数返回值向外交付结果，不暴露内部状态。
    """
    final_message: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    was_cancelled: bool = False
    stop_reason: str = "stop"


@dataclass
class AgentLoopConfig:
    """Agent Loop 配置"""
    max_turns: int | None = None
    queue_mode: str = "one_at_a_time"  # "one_at_a_time" | "all"
