"""
dot.coding.compress.l3_semantic — L3 压缩（≥85%）

LLM 生成结构化摘要，替换旧消息。
需要 LLM 调用。
"""
from __future__ import annotations

import logging
from typing import Any

from dot.ai.types import AgentMessage, AssistantMessage, TextContent, UserMessage

logger = logging.getLogger(__name__)

# 保留最近 N 条消息不动
KEEP_RECENT = 5

SUMMARY_PROMPT = """请将以下对话历史压缩为结构化摘要，保留关键信息：
1. 用户的原始需求
2. 已完成的操作和结果
3. 当前正在做什么
4. 遇到的问题和解决方案
5. 下一步计划

请用简洁的中文输出摘要。"""


async def compact_l3(
    messages: list[AgentMessage],
    *,
    provider: object | None = None,
    model: str = "gpt-4o",
    keep_recent: int = KEEP_RECENT,
) -> list[AgentMessage]:
    """L3 压缩：LLM 生成结构化摘要

    Args:
        messages: 原始消息列表
        provider: ModelProvider 实例
        model: 模型标识
        keep_recent: 保留最近的消息数

    Returns:
        压缩后的消息列表
    """
    if len(messages) <= keep_recent:
        return messages

    # 分离旧消息和新消息
    old_messages = messages[:-keep_recent]
    recent_messages = messages[-keep_recent:]

    # 构建摘要请求
    history_text = _format_history(old_messages)

    if provider is None:
        # 无 provider 时做简单截断
        summary = f"[L3 compacted] {len(old_messages)} messages compressed. Recent {keep_recent} messages preserved."
    else:
        try:
            summary = await _generate_summary(provider, model, history_text)
        except Exception as exc:
            logger.warning("[compress] L3 summary failed: %s", exc)
            summary = f"[L3 compacted] Summary generation failed ({exc}). {len(old_messages)} messages compressed."

    # 构建压缩结果
    summary_msg = UserMessage(content=f"[压缩摘要]\n{summary}")
    return [summary_msg] + recent_messages


def _format_history(messages: list[AgentMessage]) -> str:
    """格式化历史消息为文本"""
    lines = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            lines.append(f"用户: {msg.text[:200]}")
        elif isinstance(msg, AssistantMessage):
            lines.append(f"助手: {msg.text[:200]}")
    return "\n".join(lines[-50:])  # 最多取最近 50 条


async def _generate_summary(provider: object, model: str, history_text: str) -> str:
    """调用 LLM 生成摘要"""
    from dot.ai.types import AgentMessage as Msg

    messages = [
        UserMessage(content=f"{SUMMARY_PROMPT}\n\n---\n{history_text}"),
    ]

    # 简单调用 provider
    events = provider.stream_response(
        model=model,
        system="你是一个对话摘要助手。",
        messages=messages,
        tools=[],
    )

    summary = ""
    async for event in events:
        if hasattr(event, "delta"):
            summary += event.delta
        elif hasattr(event, "message"):
            summary = event.message.text
            break

    return summary or "摘要生成失败"
