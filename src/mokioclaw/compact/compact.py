"""
AutoCompact 全局压缩 + 五层流水线主入口
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from mokioclaw.compact.types import CompactConfig, CompactState
from mokioclaw.compact.snip import snip_turns
from mokioclaw.compact.micro_compact import micro_compact
from mokioclaw.compact.context_collapse import context_collapse
from mokioclaw.compact.compact_guard import (
    estimate_token,
    get_auto_compact_threshold,
    check_circuit_breaker,
    record_compact_retry,
    is_protected,
)
from mokioclaw.compact.compact_prompt import BASE_COMPACT_PROMPT, PARTIAL_COMPACT_PROMPT
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

_BOUNDARY_MARKER = "<<COMPACT_BOUNDARY>>"


# ============================================================
# 五层流水线（公开入口）
# ============================================================

def apply_compression_pipeline(
    messages: list[Any],
    config: CompactConfig,
    state: CompactState,
) -> list[Any]:
    """五层渐进式上下文压缩流水线。

    顺序严格不可调换：
      1. Snip          纯规则，过滤低价值回合
      2. MicroCompact  纯规则，大工具输出占位
      3. token check → ContextCollapse  局部阶段折叠
      4. token check → AutoCompact      全局 LLM 摘要压缩

    Args:
        messages: 原始消息列表（不会被修改，函数返回新列表）
        config: 压缩配置
        state: 压缩状态（会被原地更新）

    Returns:
        压缩后的视图消息列表
    """
    # Layer 1: Snip
    if config.enable_snip:
        messages = snip_turns(messages, config, state)

    # Layer 2: MicroCompact
    messages = micro_compact(messages, config, state)

    # Token check
    est = estimate_token(messages)
    threshold = get_auto_compact_threshold(config)

    # Layer 3: ContextCollapse（局部折叠，无 LLM）
    if est > threshold:
        messages, collapse_state = context_collapse(messages, config)
        if collapse_state.has_compacted:
            state.has_compacted = True
            state.last_boundary_index = collapse_state.last_boundary_index
            est = estimate_token(messages)
            logger.debug("After ContextCollapse: est=%d, threshold=%d", est, threshold)

    # Layer 4: AutoCompact（全局 LLM 摘要）
    if est > threshold:
        messages = _auto_compact(messages, config, state)

    return messages


# ============================================================
# AutoCompact 全局压缩
# ============================================================

def _auto_compact(messages: list[Any], config: CompactConfig, state: CompactState) -> list[Any]:
    """全局 LLM 结构化摘要压缩。

    断路器：LLM 失败 3 次后放弃压缩，回退到原始消息。
    """
    if check_circuit_breaker(config, state):
        logger.warning("AutoCompact skipped: circuit breaker tripped")
        return messages

    # 分区
    protected_end, recent_start = _find_zones(messages, config)
    if protected_end >= recent_start:
        return messages

    to_compress = messages[protected_end:recent_start]
    if not to_compress:
        return messages

    # 选择 prompt
    prompt_template = BASE_COMPACT_PROMPT if not state.has_compacted else PARTIAL_COMPACT_PROMPT
    messages_text = _format_messages_for_prompt(to_compress)
    prompt = prompt_template.format(messages=messages_text)

    # LLM 调用（含重试）
    summary_text = ""
    for attempt in range(config.max_compact_retry):
        try:
            from mokioclaw.providers.openai_provider import create_model
            model = create_model()
            response = model.invoke([HumanMessage(content=prompt)])
            summary_text = str(getattr(response, "content", "") or "").strip()
            if summary_text:
                break
        except Exception as exc:
            logger.warning("AutoCompact LLM attempt %d failed: %s", attempt + 1, exc)
            record_compact_retry(state)

    if not summary_text:
        logger.error("AutoCompact LLM failed after %d attempts, returning original", config.max_compact_retry)
        return messages

    # 组装结果：保护区 + 边界标记 + 摘要 + 最近 N 轮
    boundary_msg = SystemMessage(content=_BOUNDARY_MARKER)
    summary_msg = AIMessage(content=f"[压缩摘要]\n{summary_text}")

    result = messages[:protected_end] + [boundary_msg, summary_msg] + messages[recent_start:]

    # 最近修改的文件重新加载进上下文
    if state.recent_modified_files:
        files_ctx = _build_files_context(state.recent_modified_files)
        # 插在摘要后面、最近轮次前面
        insert_pos = len(messages[:protected_end]) + 2
        result.insert(insert_pos, SystemMessage(content=files_ctx))

    # 更新状态
    new_state = CompactState(
        has_compacted=True,
        last_boundary_index=protected_end,
        retry_count=0,
        recent_modified_files=state.recent_modified_files,
    )
    state.has_compacted = True
    state.last_boundary_index = protected_end
    state.retry_count = 0

    logger.info(
        "AutoCompact: %d → %d messages (boundary at %d)",
        len(messages), len(result), protected_end,
    )
    return result


# ============================================================
# 分区辅助
# ============================================================

def _find_zones(messages: list[Any], config: CompactConfig) -> tuple[int, int]:
    """找到保护区结束位置和最近 N 轮起始位置。

    Returns:
        (protected_end, recent_start)
        - [0, protected_end) = 保护区（不压缩）
        - [recent_start, len) = 最近 N 轮（不压缩）
        - [protected_end, recent_start) = 待压缩区
    """
    # 保护区：开头的 SystemMessage 序列
    protected_end = 0
    for i, msg in enumerate(messages):
        if is_protected(msg):
            protected_end = i + 1
        else:
            break

    # 最近 N 轮：从尾部数 HumanMessage
    recent_start = 0
    human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_indices) > config.recent_retain_turns:
        keep_from = human_indices[-config.recent_retain_turns]
        recent_start = keep_from
    else:
        recent_start = len(messages)

    return protected_end, recent_start


# ============================================================
# Prompt 辅助
# ============================================================

def _format_messages_for_prompt(messages: list[Any]) -> str:
    """将消息列表格式化为 prompt 可读文本"""
    parts: list[str] = []
    for i, msg in enumerate(messages):
        role = type(msg).__name__
        content = str(getattr(msg, "content", None) or "")
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            tc_str = ", ".join(tc.get("name", "unknown") for tc in tool_calls)
            parts.append(f"[{i}] {role}: [tool_calls: {tc_str}]")
        else:
            truncated = content[:500] + "..." if len(content) > 500 else content
            parts.append(f"[{i}] {role}: {truncated}")
    return "\n".join(parts)


def _build_files_context(file_paths: list[str]) -> str:
    """构建最近修改文件的上下文文本"""
    if not file_paths:
        return ""
    lines = ["[最近修改的文件，请参考以下内容]"]
    for path in file_paths[:5]:
        lines.append(f"- {path}")
    return "\n".join(lines)
