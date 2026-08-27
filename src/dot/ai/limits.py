"""
dot.ai.limits — 上下文窗口估算

Provider 锚定策略：Provider 报告的 usage 是权威值，
只对增量消息做字符估算。

  已知（provider 报告）| 增量（字符估算）
  ─────────────────────|──────────────────
  provider_tokens      | trailing_tokens
  (system + 历史消息)    | (新消息 + 新工具)
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import AgentMessage, AssistantMessage, ToolResultMessage

# 字符估算常量
CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_OVERHEAD_TOKENS = 16
RESERVE_TOKENS = 16384


@dataclass
class ContextWindowInfo:
    """上下文窗口信息"""
    context_window: int = 128000
    provider_tokens: int = 0
    trailing_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.provider_tokens + self.trailing_tokens

    @property
    def usage_ratio(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return self.total_tokens / self.context_window

    @property
    def should_compact_l1(self) -> bool:
        """≥50%：去掉可恢复 tool 结果"""
        return self.usage_ratio >= 0.5

    @property
    def should_compact_l2(self) -> bool:
        """≥70%：删除老旧 tool 调用"""
        return self.usage_ratio >= 0.7

    @property
    def should_compact_l3(self) -> bool:
        """≥85%：LLM 生成结构化摘要"""
        return self.usage_ratio >= 0.85

    @property
    def should_auto_compact(self) -> bool:
        """是否应自动触发压缩"""
        return self.total_tokens >= (self.context_window - RESERVE_TOKENS)


def estimate_message_tokens(message: AgentMessage) -> int:
    """估算单条消息的 token 数"""
    text = ""
    tool_overhead = 0

    if isinstance(message, AssistantMessage):
        text = message.text
        tool_overhead = len(message.tool_calls) * TOOL_OVERHEAD_TOKENS
    elif isinstance(message, ToolResultMessage):
        text = message.text
        tool_overhead = TOOL_OVERHEAD_TOKENS
    elif hasattr(message, "text"):
        text = message.text

    char_count = len(text)
    return MESSAGE_OVERHEAD_TOKENS + (char_count + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + tool_overhead


def estimate_trailing_tokens(messages: list[AgentMessage], *, after_index: int = 0) -> int:
    """估算从指定索引开始的增量消息 token 数"""
    total = 0
    for msg in messages[after_index:]:
        total += estimate_message_tokens(msg)
    return total


def estimate_context_tokens(
    messages: list[AgentMessage],
    *,
    provider_tokens: int = 0,
    context_window: int = 128000,
    after_index: int = 0,
) -> ContextWindowInfo:
    """估算上下文 token 使用量

    Args:
        messages: 消息历史
        provider_tokens: Provider 上次报告的 token 数（权威值）
        context_window: 模型上下文窗口大小
        after_index: 从哪条消息开始估算增量（之前的消息已包含在 provider_tokens 中）

    Returns:
        ContextWindowInfo: 上下文窗口使用信息
    """
    trailing = estimate_trailing_tokens(messages, after_index=after_index)
    return ContextWindowInfo(
        context_window=context_window,
        provider_tokens=provider_tokens,
        trailing_tokens=trailing,
    )
