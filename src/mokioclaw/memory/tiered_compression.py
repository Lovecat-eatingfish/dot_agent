"""
分级压缩策略实现

Claude Code 风格：智能选择保留/压缩/删除的内容

压缩级别：
- KEEP_ALWAYS: 永远不压缩（用户指令、错误信息、关键文件）
- COMPRESS_LIGHTLY: 轻度压缩（只删除冗余输出）
- COMPRESS_HEAVILY: 重度压缩（保留核心信息，删除细节）
- DROP: 直接删除（旧的中间过程）
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


# ========== 分级策略配置 ==========

# 永远保留的消息类型
KEEP_ALWAYS_PRIORITY = 100

# 轻度压缩（只删除长输出）
COMPRESS_LIGHTLY_PRIORITY = 50

# 重度压缩（保留摘要）
COMPRESS_HEAVILY_PRIORITY = 20

# 直接删除
DROP_PRIORITY = 0


def classify_message_for_compression(msg: Any, *, node: str = "unknown") -> int:
    """对消息进行分类，决定压缩策略

    Args:
        msg: LangChain 消息对象
        node: 当前节点名称（planner/codeAgent/verifier 等）

    Returns:
        优先级分数（0-100），越高越重要
    """
    msg_type = type(msg).__name__

    # 1. SystemMessage 包含工具描述 - 必须保留
    if isinstance(msg, SystemMessage):
        # 检查是否是工具描述 system prompt
        content = str(msg.content or "")
        if "You are" in content or "Available tools:" in content or "Rules:" in content:
            return KEEP_ALWAYS_PRIORITY
        return COMPRESS_LIGHTLY_PRIORITY

    # 2. HumanMessage - 用户指令必须保留
    if isinstance(msg, HumanMessage):
        content = str(msg.content or "")
        # 如果是分层 memory prompt，保留但可以压缩
        if '"rules"' in content and '"working_memory"' in content:
            return COMPRESS_LIGHTLY_PRIORITY
        # 普通用户消息 - 永远保留
        return KEEP_ALWAYS_PRIORITY

    # 3. ToolMessage - 工具结果，根据内容决定
    if isinstance(msg, ToolMessage):
        content = str(msg.content or "")
        tool_name = getattr(msg, "name", None) or ""

        # BashTool 超长输出 - 重度压缩
        if tool_name == "BashTool":
            if len(content) > 2000:
                return COMPRESS_HEAVILY_PRIORITY
            return COMPRESS_LIGHTLY_PRIORITY

        # FileReadTool - 文件内容可能很大
        if tool_name == "FileReadTool":
            if len(content) > 3000:
                return COMPRESS_HEAVILY_PRIORITY
            return COMPRESS_LIGHTLY_PRIORITY

        # WebSearchTool - 搜索结果
        if tool_name == "WebSearchTool":
            return COMPRESS_LIGHTLY_PRIORITY

        # 其他工具 - 轻度压缩
        return COMPRESS_LIGHTLY_PRIORITY

    # 4. AIMessage - 模型输出
    if isinstance(msg, AIMessage):
        content = str(msg.content or "")

        # 包含 tool_calls - 中间过程，可以压缩
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            # 如果 tool_calls 很多，说明在执行复杂任务
            if len(msg.tool_calls) > 5:
                return COMPRESS_HEAVILY_PRIORITY
            return COMPRESS_LIGHTLY_PRIORITY

        # 空消息 - 删除
        if not content.strip():
            return DROP_PRIORITY

        # 普通 AI 回复 - 轻度压缩
        return COMPRESS_LIGHTLY_PRIORITY

    # 默认：轻度压缩
    return COMPRESS_LIGHTLY_PRIORITY


def compress_messages_by_tier(messages: list[Any], *, context_summary: str = "") -> list[Any]:
    """按分级策略压缩消息列表

    策略：
    1. KEEP_ALWAYS (100): 原样保留
    2. COMPRESS_LIGHTLY (50): 只截断超长消息
    3. COMPRESS_HEAVILY (20): 替换为摘要
    4. DROP (0): 删除

    保持消息原始顺序，且保护 AIMessage(tool_calls) 与 ToolMessage 的配对：
    - 父 AIMessage 被摘要/删除时，其对应的 ToolMessage 一并移除（否则悬空 tool message 会触发 API 400）
    - 父 AIMessage 保留时，其 ToolMessage 至少截断保留，绝不移除（截断时保留 tool_call_id）

    Args:
        messages: 原始消息列表
        context_summary: 上下文摘要（用于替换被删除的消息）

    Returns:
        压缩后的消息列表
    """
    if not messages:
        return messages

    classified: list[tuple[int, Any]] = [(classify_message_for_compression(msg), msg) for msg in messages]

    # 收集将被移除（摘要/DROP）的 AIMessage 的 tool_call_id，
    # 这些 id 对应的 ToolMessage 必须一起移除，避免悬空。
    removed_parent_call_ids: set[str] = set()
    for score, msg in classified:
        if score < COMPRESS_LIGHTLY_PRIORITY and isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                call_id = str(tc.get("id") or "")
                if call_id:
                    removed_parent_call_ids.add(call_id)

    result: list[Any] = []
    heavily_buffer: list[Any] = []
    first_heavily_idx: int | None = None
    counts = {"keep": 0, "lightly": 0, "heavily": 0, "drop": 0}

    for score, msg in classified:
        # 悬空 ToolMessage：父 AIMessage 已被摘要/删除 → 一并丢弃（计入 heavily）
        if isinstance(msg, ToolMessage) and str(getattr(msg, "tool_call_id", "") or "") in removed_parent_call_ids:
            heavily_buffer.append(msg)
            counts["heavily"] += 1
            continue

        if score >= KEEP_ALWAYS_PRIORITY:
            result.append(msg)
            counts["keep"] += 1
        elif score >= COMPRESS_LIGHTLY_PRIORITY:
            result.append(_truncate_message_if_needed(msg))
            counts["lightly"] += 1
        elif score >= COMPRESS_HEAVILY_PRIORITY:
            if first_heavily_idx is None:
                first_heavily_idx = len(result)
            heavily_buffer.append(msg)
            counts["heavily"] += 1
        else:
            counts["drop"] += 1

    # 摘要插到第一条被摘要消息原来的位置，保持时间线可读
    if heavily_buffer:
        summary = _create_compression_summary(heavily_buffer, context_summary)
        result.insert(first_heavily_idx if first_heavily_idx is not None else len(result), summary)

    logger.debug(
        "compression: keep=%d, lightly=%d, heavily=%d, drop=%d → %d total",
        counts["keep"],
        counts["lightly"],
        counts["heavily"],
        counts["drop"],
        len(result),
    )

    return result


def _truncate_message_if_needed(msg: Any, max_chars: int = 2000) -> Any:
    """截断超长的消息（仅 ToolMessage）

    Args:
        msg: 消息对象
        max_chars: 最大字符数

    Returns:
        截断后的消息（如果需要）
    """
    if not isinstance(msg, ToolMessage):
        return msg

    content = str(msg.content or "")
    if len(content) <= max_chars:
        return msg

    truncated = content[:max_chars] + f"\n\n... [Output truncated, full version saved to file]"
    return ToolMessage(
        content=truncated,
        name=msg.name,
        id=msg.id,
        tool_call_id=msg.tool_call_id,
    )


def _create_compression_summary(compressed_msgs: list[Any], context_summary: str) -> AIMessage:
    """创建压缩摘要消息

    将多个被重度压缩的消息合并为一个摘要。

    Args:
        compressed_msgs: 被压缩的消息列表
        context_summary: 上下文摘要

    Returns:
        摘要消息
    """
    summary_parts = []

    # 统计信息
    tool_calls = []
    bash_outputs = []
    file_reads = []

    for msg in compressed_msgs:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(tc.get("name", "unknown"))
        elif isinstance(msg, ToolMessage):
            tool_name = getattr(msg, "name", "") or ""
            if tool_name == "BashTool":
                bash_outputs.append(str(msg.content or "")[:200])
            elif tool_name == "FileReadTool":
                file_reads.append(str(msg.content or "")[:200])

    # 构建摘要
    if tool_calls:
        summary_parts.append(f"**Tool calls ({len(tool_calls)})**: {', '.join(tool_calls[:5])}")
    if bash_outputs:
        summary_parts.append(f"**Bash outputs**: {len(bash_outputs)} commands executed")
    if file_reads:
        summary_parts.append(f"**File reads**: {len(file_reads)} files read")

    # 添加上下文摘要
    if context_summary:
        summary_parts.append(f"**Context**: {context_summary[:500]}")

    summary_text = "\n".join(summary_parts) if summary_parts else "[Compressed content]"

    return AIMessage(content=f"[Compressed: {len(compressed_msgs)} messages]\n{summary_text}")


def estimate_tokens_for_tiered_compression(
    messages: list[Any],
    context_summary: str = "",
    *,
    compressed_messages: list[Any] | None = None,
) -> dict[str, int]:
    """预估分级压缩后的 token 数

    Args:
        messages: 原始消息列表
        context_summary: 上下文摘要
        compressed_messages: 已压缩的消息列表（避免重复压缩）

    Returns:
        预估统计：
        - original_tokens: 原始 token 数
        - compressed_tokens: 压缩后 token 数
        - reduction_tokens: 减少的 token 数
        - reduction_pct: 减少百分比
    """
    # 粗略估算：每个字符 ≈ 0.25 tokens
    def estimate(text: str) -> int:
        # 精确一点：4 个字符 = 1 token（向上取整）
        return max(0, (len(text) + 3) // 4)

    original = sum(estimate(str(m.content or "")) for m in messages if hasattr(m, "content"))

    # 复用已压缩的消息，避免重复压缩
    if compressed_messages is None:
        compressed_messages = compress_messages_by_tier(messages, context_summary=context_summary)

    compressed_tokens = sum(estimate(str(m.content or "")) for m in compressed_messages if hasattr(m, "content"))

    reduction = original - compressed_tokens
    pct = (reduction / original * 100) if original > 0 else 0

    return {
        "original_tokens": original,
        "compressed_tokens": compressed_tokens,
        "reduction_tokens": reduction,
        "reduction_pct": round(pct, 1),
    }
