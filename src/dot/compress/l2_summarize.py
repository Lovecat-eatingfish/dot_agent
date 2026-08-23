"""
L2 滑窗摘要 — 每 N 轮触发

对旧消息生成摘要，替换原始消息为单条摘要消息。
设计约束（对齐设计文档）：
  - 摘要窗口 = 10 轮
  - 保留最近 3 轮消息不动
  - 摘要替换旧消息
  - 超时/失败：跳过 L2（fail-open）
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from ..core.log import get_logger
from ..core.llm import create_model
from ._utils import messages_to_text

logger = get_logger(__name__)

# L2 保留最近 N 轮不动
L2_KEEP_RECENT_TURNS = 3

# L2 摘要最大 token 数
L2_MAX_SUMMARY_CHARS = 5_000

L2_SYSTEM_PROMPT = """You are a conversation summarizer. Your job is to create a concise summary of a conversation history that will replace the original messages.

The summary should:
1. Preserve key technical decisions and their rationale
2. Record file operations (read/write/edit) with paths
3. Capture errors encountered and their solutions
4. Maintain task context (what was being done, what's next)
5. Preserve user preferences and constraints

Rules:
- Output a structured summary in plain text
- Use sections: ## Task Context, ## Key Decisions, ## File Operations, ## Errors & Fixes, ## Next Steps
- Maximum 800 words
- Be specific (include file paths, function names, error messages)
- Do NOT include conversation pleasantries
- The summary will be used as context for continuing the conversation
"""

L2_USER_PROMPT = """Summarize this conversation history:

{conversation}

Output a structured summary that preserves all important context."""


def summarize_window(
    messages: list[Any],
    session: Any = None,
) -> tuple[list[Any], str]:
    """对旧消息生成摘要，返回 (压缩后的消息列表, 摘要文本)

    Args:
        messages: 完整消息列表
        session: 可选 Session（用于链路追踪）

    Returns:
        (压缩后的消息列表, 摘要文本)
        如果摘要失败，返回 (原始消息, "")
    """
    if not messages:
        return messages, ""

    # 分离 system messages 和非 system messages
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    if len(other_msgs) <= L2_KEEP_RECENT_TURNS * 3:
        # 消息太少，不需要摘要（*3 因为工具调用序列是 3 条消息）
        return messages, ""

    # 分割：旧消息（需要摘要） + 最近消息（保留）
    # 使用 *3 而不是 *2，因为工具调用序列是 3 条消息（AI with tool_calls → ToolMessage → AI response）
    split_point = max(0, len(other_msgs) - L2_KEEP_RECENT_TURNS * 3)
    old_msgs = other_msgs[:split_point]
    recent_msgs = other_msgs[split_point:]

    # 确保分割点不在工具调用序列中间
    # 工具调用序列是 3 条消息：AI with tool_calls → ToolMessage → AI response
    # 如果 recent_msgs 以 ToolMessage 开头，说明分割点在工具调用序列中间，需要向前调整
    while recent_msgs and isinstance(recent_msgs[0], ToolMessage):
        split_point -= 1
        old_msgs = other_msgs[:split_point]
        recent_msgs = other_msgs[split_point:]

    # 如果 old_msgs 以 AIMessage with tool_calls 结尾，说明分割点在工具调用序列中间，需要向后调整
    while old_msgs and hasattr(old_msgs[-1], 'tool_calls') and old_msgs[-1].tool_calls:
        split_point += 1
        old_msgs = other_msgs[:split_point]
        recent_msgs = other_msgs[split_point:]

    if not old_msgs:
        return messages, ""

    # 转换为文本
    conversation_text = messages_to_text(old_msgs)
    if not conversation_text.strip():
        return messages, ""

    # 调用 LLM 生成摘要
    try:
        model = create_model()
        response = model.invoke([
            SystemMessage(content=L2_SYSTEM_PROMPT),
            HumanMessage(content=L2_USER_PROMPT.format(conversation=conversation_text)),
        ])
        summary = getattr(response, "content", "")
        if not summary:
            logger.warning("[L2] LLM returned empty summary")
            return messages, ""

        # 截断过长摘要
        if len(summary) > L2_MAX_SUMMARY_CHARS:
            summary = summary[:L2_MAX_SUMMARY_CHARS] + "\n... [truncated]"

        # 构造压缩后的消息列表
        summary_msg = HumanMessage(content=f"[Conversation Summary]\n{summary}")
        compressed = system_msgs + [summary_msg] + recent_msgs

        logger.info(
            "[L2] summarized %d messages → 1 summary + %d recent = %d total",
            len(old_msgs), len(recent_msgs), len(compressed),
        )
        return compressed, summary

    except Exception as exc:
        logger.warning("[L2] summarization failed (fail-open): %s", exc)
        return messages, ""
