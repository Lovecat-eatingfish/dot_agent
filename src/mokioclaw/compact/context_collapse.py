"""
ContextCollapse 层：局部阶段折叠（非全局替换，无 LLM）
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mokioclaw.compact.types import CompactConfig, CompactState
from mokioclaw.compact.compact_guard import is_protected
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


def context_collapse(messages: list[Any], config: CompactConfig) -> tuple[list[Any], CompactState]:
    """局部阶段折叠。

    不删除原始消息，只给历史消息打 is_folded 标记，插入一段局部结构化摘要。
    只折叠单个任务阶段，区别于 AutoCompact 全局替换。
    """
    if len(messages) < 4:
        return messages, CompactState()

    # 找折叠区：跳过保护区，保留最后 N 轮
    fold_start = 0
    for i, msg in enumerate(messages):
        if not is_protected(msg):
            fold_start = i
            break

    fold_end = len(messages)
    human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_indices) > config.recent_retain_turns:
        keep_from = human_indices[-config.recent_retain_turns]
        fold_end = keep_from

    if fold_end - fold_start < 2:
        return messages, CompactState()

    # 构建摘要
    fold_region = messages[fold_start:fold_end]
    summary_text = _build_summary(fold_region)

    # 标记折叠
    for msg in fold_region:
        additional = getattr(msg, "additional_kwargs", {}) or {}
        additional["_is_folded"] = True
        msg.additional_kwargs = additional

    # 插入摘要消息
    summary_msg = AIMessage(content=f"[ContextCollapse: {summary_text}]")
    result = messages[:fold_start] + [summary_msg] + messages[fold_end:]

    state = CompactState(
        has_compacted=True,
        last_boundary_index=fold_start,
    )
    logger.info("ContextCollapse: folded %d messages at index %d", fold_end - fold_start, fold_start)
    return result, state


def _build_summary(messages: list[Any]) -> str:
    """规则式构建局部摘要"""
    tool_calls: list[str] = []
    findings: list[str] = []

    for msg in messages:
        if isinstance(msg, AIMessage):
            tcs = getattr(msg, "tool_calls", None) or []
            for tc in tcs:
                name = tc.get("name", "unknown")
                if name not in tool_calls:
                    tool_calls.append(name)
            content = str(getattr(msg, "content", None) or "").strip()
            if content and not tcs:
                findings.append(content[:200])
        elif isinstance(msg, ToolMessage):
            content = str(getattr(msg, "content", None) or "").strip()
            if content:
                findings.append(content[:150])

    parts: list[str] = []
    if tool_calls:
        parts.append(f"工具调用: {', '.join(tool_calls[:8])}")
    if findings:
        parts.append(f"关键结果: {'; '.join(findings[:5])}")

    return " | ".join(parts) if parts else "[已折叠的对话片段]"
