"""
Snip 压缩层（对齐 Claude Code HISTORY_SNIP / snipCompactIfNeeded）

在 microcompact 之后、LLM autocompact 之前运行的轻量裁剪：
- 丢弃较早的 tool_result 正文，保留调用轨迹占位
- 不调用 LLM，成本为零
- 返回 tokens_freed 估算，供 autocompact 阈值计算
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 保留最近 N 条完整 tool_result；更早的替换为占位
_DEFAULT_KEEP_RECENT_TOOLS = 8
_SNIP_PLACEHOLDER = "[snip] Earlier tool output removed to free context. Re-run the tool if needed."


def snip_compact_if_needed(
    messages: list[Any],
    *,
    keep_recent_tools: int = _DEFAULT_KEEP_RECENT_TOOLS,
    min_messages: int = 24,
) -> tuple[list[Any], int]:
    """轻量 snip；返回 (messages, tokens_freed 估算)

    仅当消息数超过 min_messages 时触发。
    """
    if len(messages) < min_messages:
        return messages, 0

    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_indices) <= keep_recent_tools:
        return messages, 0

    snip_set = set(tool_indices[:-keep_recent_tools])
    tokens_freed = 0
    out: list[Any] = []
    for i, msg in enumerate(messages):
        if i not in snip_set:
            out.append(msg)
            continue
        content = getattr(msg, "content", "") or ""
        content_len = len(str(content))
        if content_len < 80:
            out.append(msg)
            continue
        tokens_freed += max(1, content_len // 4)
        out.append(
            ToolMessage(
                content=_SNIP_PLACEHOLDER,
                name=getattr(msg, "name", None) or "tool",
                tool_call_id=getattr(msg, "tool_call_id", "") or f"snip-{i}",
                id=getattr(msg, "id", None),
            )
        )

    if tokens_freed:
        logger.info("snip compacted %d tool results, ~%d tokens freed", len(snip_set), tokens_freed)
    return out, tokens_freed


def reactive_compact_messages(messages: list[Any], *, keep_last: int = 8) -> list[Any]:
    """Reactive Compact：autocompact 连续失败后的激进丢弃（对齐 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES）"""
    if len(messages) <= keep_last + 1:
        return messages
    head = [m for m in messages[:2] if m.__class__.__name__ == "SystemMessage"]
    if not head and messages:
        head = [messages[0]]
    marker = HumanMessage(
        content=(
            f"[reactive compact] Dropped {len(messages) - len(head) - keep_last} messages "
            "after repeated autocompact failures. Re-read files and continue."
        )
    )
    return head + [marker] + list(messages[-keep_last:])
