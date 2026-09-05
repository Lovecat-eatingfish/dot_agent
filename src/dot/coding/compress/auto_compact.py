"""
dot.coding.compress.auto_compact — AutoCompactor（agent 循环的自动压缩钩子）

实现 agent 层的 CompactionGate 契约：在每轮 turn 边界被调用，
估算上下文占用，超过阈值时执行压缩（L1/L2/L3 全部内联 await，
保证在下一轮 LLM 调用前生效，不与运行中的回合并发）。

防抖：压缩后记录当时的占用基线，占用没有重新增长之前不再重复触发，
避免 L1 提示文本被反复截断叠加。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from dot.ai.limits import ContextWindowInfo

from .compactor import ContextCompactor

if TYPE_CHECKING:
    from dot.agent.compaction import CompactionResult
    from dot.ai.types import AgentMessage

logger = logging.getLogger(__name__)

DEFAULT_AUTO_COMPACT_RATIO = 0.7


class AutoCompactor:
    """自动压缩钩子：占用 ≥ 阈值时同步执行压缩

    阈值默认 0.7（L2 档）：一次触发同时应用 L1+L2；
    达到 0.85（L3 档）时内联 await LLM 摘要。
    """

    def __init__(
        self,
        compactor: ContextCompactor,
        *,
        threshold_ratio: float = DEFAULT_AUTO_COMPACT_RATIO,
    ) -> None:
        self._compactor = compactor
        self._threshold_ratio = threshold_ratio
        self._last_compacted_tokens: int | None = None

    async def maybe_compact(self, messages: list["AgentMessage"]) -> "CompactionResult | None":
        """检查占用，超阈值时压缩并返回结果；否则返回 None"""
        from dot.agent.compaction import CompactionResult

        info = self._compactor.estimate(messages)
        if not self._should_compact(info):
            return None

        before_count = len(messages)
        outcome = await self._compactor.compact_async(messages)
        if not outcome.applied:
            return None

        self._last_compacted_tokens = self._compactor.estimate(outcome.messages).total_tokens
        reason = outcome.report or f"usage {info.usage_ratio:.0%}"
        logger.info("[auto-compact] %s (%d -> %d messages)",
                    outcome.level, before_count, len(outcome.messages))
        return CompactionResult(
            messages=outcome.messages,
            level=outcome.level,
            reason=reason,
        )

    def _should_compact(self, info: ContextWindowInfo) -> bool:
        """触发条件：占用率 ≥ 阈值，且自上次压缩后占用有重新增长"""
        if info.usage_ratio < self._threshold_ratio:
            return False
        if self._last_compacted_tokens is not None and info.total_tokens <= self._last_compacted_tokens:
            return False
        return True


__all__ = ["AutoCompactor", "DEFAULT_AUTO_COMPACT_RATIO"]
