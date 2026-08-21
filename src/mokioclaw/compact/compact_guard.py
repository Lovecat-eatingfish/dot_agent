"""
token 估算、保护区判断、断路器、阈值计算
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import SystemMessage

from mokioclaw.compact.types import CompactConfig, CompactState
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

_SAFETY_FACTOR = 1.3


# ============================================================
# Token 估算
# ============================================================

def estimate_token(messages: list[Any]) -> int:
    """粗略 token 估算：4 字符 ≈ 1 token，再乘安全系数 1.3"""
    total_chars = 0
    for m in messages:
        content = getattr(m, "content", None) or ""
        if isinstance(content, str):
            total_chars += len(content)
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            args = tc.get("args", {}) or {}
            total_chars += len(str(args))
    raw = max(0, (total_chars + 3) // 4)
    return int(raw * _SAFETY_FACTOR)


def get_auto_compact_threshold(config: CompactConfig) -> int:
    """压缩触发阈值"""
    return config.max_context_window - config.output_reserve - config.safety_buffer


# ============================================================
# 保护区判断
# ============================================================

def is_protected(msg: Any) -> bool:
    """判断单条消息是否处于保护区（不可压缩）"""
    if isinstance(msg, SystemMessage):
        return True
    additional = getattr(msg, "additional_kwargs", {}) or {}
    if additional.get("_is_folded"):
        return True
    if additional.get("_is_compacted"):
        return True
    if additional.get("_after_compact_boundary"):
        return True
    return False


# ============================================================
# 断路器
# ============================================================

def check_circuit_breaker(config: CompactConfig, state: CompactState) -> bool:
    """检查断路器是否触发。触发返回 True，此时应放弃 LLM 压缩回退规则裁剪。"""
    if state.retry_count >= config.max_compact_retry:
        logger.warning(
            "Compact circuit breaker tripped: retries=%d >= max=%d",
            state.retry_count,
            config.max_compact_retry,
        )
        state.has_compacted = True  # 标记会话关闭自动压缩
        return True
    return False


def record_compact_retry(state: CompactState) -> None:
    """记录一次压缩失败"""
    state.retry_count += 1


def reset_compact_retry(state: CompactState) -> None:
    """压缩成功后重置计数器"""
    state.retry_count = 0
