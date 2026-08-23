"""
L3 语义压缩 — 接近窗口限时触发

按重要性评分丢弃低价值消息，直到 token 数 ≤ 目标值。
设计约束（对齐设计文档）：
  - 重要性评分：tool 调用结果 > 用户指令 > 普通对话
  - 最近 3 轮消息不压缩
  - 包含错误信息的消息权重 +0.3
  - 包含文件路径的消息权重 +0.2
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from ..core.log import get_logger
from ._utils import extract_content

logger = get_logger(__name__)

# L3 保留最近 N 条消息（不压缩）
L3_KEEP_RECENT = 10

# 重要性评分权重
IMPORTANCE_WEIGHTS = {
    "tool_result": 0.8,      # 工具调用结果
    "user_instruction": 0.7,  # 用户指令
    "ai_with_tools": 0.6,    # AI 调用工具的消息
    "ai_response": 0.4,      # AI 普通回复
    "error_bonus": 0.3,      # 包含错误信息的加分
    "file_path_bonus": 0.2,  # 包含文件路径的加分
}

# 文件路径正则
_FILE_PATH_PATTERN = re.compile(
    r'(?:[A-Za-z]:\\|/)?(?:[\w.-]+[/\\])+[\w.-]+\.\w+'
    r'|(?:\./|\.\./)[\w./\\-]+\.\w+'
)

# 错误关键词
_ERROR_KEYWORDS = {"error", "exception", "failed", "traceback", "错误", "失败", "异常"}


def semantic_compress(
    messages: list[Any],
    target_tokens: int,
    estimate_tokens_fn: Any,
    session: Any = None,
) -> list[Any]:
    """按重要性评分丢弃低价值消息，直到 token 数 ≤ 目标值

    Args:
        messages: 完整消息列表
        target_tokens: 目标 token 数
        estimate_tokens_fn: token 估算函数 (messages) -> int
        session: 可选 Session

    Returns:
        压缩后的消息列表
    """
    if not messages:
        return messages

    current_tokens = estimate_tokens_fn(messages)
    if current_tokens <= target_tokens:
        return messages

    # 分离 system messages（不压缩）
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    if len(other_msgs) <= L3_KEEP_RECENT:
        return messages

    # 分割：可压缩消息 + 保留消息
    compressible = other_msgs[:-L3_KEEP_RECENT]
    protected = other_msgs[-L3_KEEP_RECENT:]

    if not compressible:
        return messages

    # 计算每条消息的重要性评分
    scored = []
    for i, msg in enumerate(compressible):
        score = _calculate_importance(msg, i, len(compressible))
        scored.append((score, i, msg))

    # 按评分排序（低分在前，优先丢弃）
    scored.sort(key=lambda x: x[0])

    # 逐步丢弃低分消息，直到 token 数 ≤ 目标
    remaining = list(scored)
    tokens_to_save = current_tokens - target_tokens
    saved_tokens = 0
    removed_indices = set()

    for score, idx, msg in remaining:
        if saved_tokens >= tokens_to_save:
            break
        msg_tokens = estimate_tokens_fn([msg])
        # 不丢弃高分消息（评分 > 0.6）
        if score > 0.6 and saved_tokens > 0:
            continue
        removed_indices.add(idx)
        saved_tokens += msg_tokens

    # 构造压缩后的消息列表
    compressed = system_msgs + [
        msg for i, msg in enumerate(compressible)
        if i not in removed_indices
    ] + protected

    logger.info(
        "[L3] semantic compress: %d → %d messages (removed %d low-value, saved ~%d tokens)",
        len(messages), len(compressed), len(removed_indices), saved_tokens,
    )
    return compressed


def _calculate_importance(msg: Any, index: int, total: int) -> float:
    """计算单条消息的重要性评分

    评分规则：
    - tool 调用结果 > 用户指令 > 普通对话
    - 包含错误信息 +0.3
    - 包含文件路径 +0.2
    - 位置越新越重要（线性衰减）
    """
    score = 0.0
    content = extract_content(msg)

    # 基础分（按消息类型）
    if isinstance(msg, ToolMessage):
        score = IMPORTANCE_WEIGHTS["tool_result"]
    elif isinstance(msg, HumanMessage):
        score = IMPORTANCE_WEIGHTS["user_instruction"]
    elif isinstance(msg, AIMessage):
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            score = IMPORTANCE_WEIGHTS["ai_with_tools"]
        else:
            score = IMPORTANCE_WEIGHTS["ai_response"]

    # 错误信息加分
    content_lower = content.lower()
    if any(kw in content_lower for kw in _ERROR_KEYWORDS):
        score += IMPORTANCE_WEIGHTS["error_bonus"]

    # 文件路径加分
    if _FILE_PATH_PATTERN.search(content):
        score += IMPORTANCE_WEIGHTS["file_path_bonus"]

    # 位置权重（越新越重要，线性衰减）
    if total > 1:
        position_weight = 0.5 + 0.5 * (index / (total - 1))
        score *= position_weight

    return min(score, 1.0)
