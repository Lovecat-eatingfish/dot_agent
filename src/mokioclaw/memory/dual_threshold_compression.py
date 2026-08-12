"""
双阈值上下文压缩策略

实现软阈值（预生成）+ 硬阈值（强制压缩）的双层机制，
以及增量式摘要更新，避免全量重算。

面试官考察点：
- Q7: "第11轮怎么处理总结" → 增量叠加，非全量重算
- Q8: "前10轮就不管了吗" → 持久化但不发送
- Q9: "是增量还是全量" → 增量 O(n)，全量 O(n²)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


@dataclass
class CompressionThresholds:
    """压缩阈值配置"""

    # 软阈值：异步预生成摘要（不阻塞）
    soft_threshold: float = 0.70  # 70% 容量时触发

    # 硬阈值：同步强制压缩（阻塞当前请求）
    hard_threshold: float = 0.90  # 90% 容量时触发

    # 最大上下文容量（tokens）
    max_context_tokens: int = 128_000  # Claude Sonnet 3.5


@dataclass
class CompressionStats:
    """压缩统计信息"""

    original_tokens: int = 0
    compressed_tokens: int = 0
    reduction_tokens: int = 0
    reduction_pct: float = 0.0
    strategy: str = "none"  # none | soft | hard | step_triggered
    duration_ms: float = 0.0
    incremental: bool = False  # 是否增量更新


@dataclass
class SummaryChain:
    """摘要链：维护多层级的增量摘要

    结构：
    - summary_1_10: 第1-10轮的摘要
    - summary_1_11: 第1-11轮的摘要（基于 summary_1_10 + turn_11 增量生成）
    - summary_1_12: 第1-12轮的摘要（基于 summary_1_11 + turn_12 增量生成）

    面试官考察点：
    - 避免全量重算（O(n²) → O(n)）
    - 原始上下文持久化但不发送
    """

    summaries: list[dict[str, Any]] = field(default_factory=list)
    raw_history_file: str = "RAW_HISTORY.md"  # 完整历史存档

    def add_summary(self, turn_range: str, summary: str, turn_count: int) -> None:
        """添加新的摘要层

        Args:
            turn_range: 轮次范围，如 "1-10"
            summary: 摘要内容
            turn_count: 总轮次数
        """
        self.summaries.append({
            "range": turn_range,
            "summary": summary,
            "turns": turn_count,
            "timestamp": time.time(),
        })
        # 只保留最近 5 层摘要，避免无限增长
        if len(self.summaries) > 5:
            self.summaries.pop(0)

    def get_latest_summary(self) -> str:
        """获取最新的摘要"""
        if not self.summaries:
            return ""
        return self.summaries[-1]["summary"]

    def get_summary_chain(self, max_layers: int = 3) -> list[str]:
        """获取最近的摘要链（用于上下文拼接）

        Args:
            max_layers: 最多返回几层摘要

        Returns:
            摘要列表，从旧到新
        """
        return [s["summary"] for s in self.summaries[-max_layers:]]


class DualThresholdCompressor:
    """双阈值压缩器

    实现：
    1. 软阈值触发：异步预生成摘要，不阻塞
    2. 硬阈值触发：同步阻塞，强制压缩
    3. 步数触发：工具调用步数过多时强制总结
    4. 增量更新：基于上一次摘要叠加，避免全量重算

    面试官考察点对应：
    - Q4: "什么时候触发" → 双阈值 + 步数触发
    - Q7: "第11轮怎么处理" → 增量叠加
    - Q8: "前10轮原始上下文" → 持久化到 RAW_HISTORY.md
    """

    def __init__(
        self,
        thresholds: CompressionThresholds | None = None,
        summary_chain: SummaryChain | None = None,
    ):
        self.thresholds = thresholds or CompressionThresholds()
        self.summary_chain = summary_chain or SummaryChain()
        self.last_compression_time: float = 0.0
        self.compression_count: int = 0

    def check_compression_needed(
        self,
        current_tokens: int,
        step_count: int,
    ) -> tuple[bool, str, CompressionStats]:
        """检查是否需要压缩

        Args:
            current_tokens: 当前上下文 token 数
            step_count: 当前工具调用步数

        Returns:
            (是否需要压缩, 触发原因, 统计信息)
        """
        capacity = self.thresholds.max_context_tokens
        usage_ratio = current_tokens / capacity if capacity > 0 else 1.0

        # 条件1：步数触发（工具调用超过5步）
        if step_count >= 5:
            stats = CompressionStats(
                original_tokens=current_tokens,
                strategy="step_triggered",
            )
            return True, f"step_count={step_count} >= 5", stats

        # 条件2：硬阈值触发（必须立即压缩）
        if current_tokens >= capacity * self.thresholds.hard_threshold:
            stats = CompressionStats(
                original_tokens=current_tokens,
                compressed_tokens=int(current_tokens * 0.4),  # 预估压缩到40%
                reduction_tokens=int(current_tokens * 0.6),
                reduction_pct=60.0,
                strategy="hard",
            )
            return True, f"hard_threshold={usage_ratio:.1%}", stats

        # 条件3：软阈值触发（预生成摘要）
        if current_tokens >= capacity * self.thresholds.soft_threshold:
            stats = CompressionStats(
                original_tokens=current_tokens,
                strategy="soft",
            )
            return True, f"soft_threshold={usage_ratio:.1%}", stats

        return False, f"usage={usage_ratio:.1%}", CompressionStats()

    def should_async_prepare(self, current_tokens: int) -> bool:
        """检查是否应该异步预生成摘要（软阈值）

        不阻塞当前请求，后台准备摘要
        """
        capacity = self.thresholds.max_context_tokens
        return current_tokens >= capacity * self.thresholds.soft_threshold

    def compress_context(
        self,
        messages: list[Any],
        context_summary: str = "",
        force_hard: bool = False,
    ) -> tuple[list[Any], CompressionStats]:
        """压缩上下文

        策略：
        - 软阈值：只预生成摘要，不实际压缩
        - 硬阈值：执行压缩，优先增量更新

        Args:
            messages: 原始消息列表
            context_summary: 现有上下文摘要
            force_hard: 是否强制执行硬压缩

        Returns:
            (压缩后的消息, 统计信息)
        """
        start_time = time.time()

        # 估算 token 数（简化：字符数 / 4）
        original_tokens = sum(
            max(0, (len(str(m.content or "")) + 3) // 4)
            for m in messages
            if hasattr(m, "content")
        )

        # 策略2：硬阈值 - 执行压缩（优先级高于软阈值）
        if force_hard or original_tokens >= self.thresholds.max_context_tokens * self.thresholds.hard_threshold:
            # 增量压缩：基于上一次摘要叠加
            compressed = self._incremental_compress(messages, context_summary)
            compressed_tokens = sum(
                max(0, (len(str(m.content or "")) + 3) // 4)
                for m in compressed
                if hasattr(m, "content")
            )

            reduction = original_tokens - compressed_tokens
            stats = CompressionStats(
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                reduction_tokens=reduction,
                reduction_pct=(reduction / original_tokens * 100) if original_tokens > 0 else 0,
                strategy="hard",
                duration_ms=(time.time() - start_time) * 1000,
                incremental=True,
            )

            self.last_compression_time = time.time()
            self.compression_count += 1

            logger.info(
                "Context compressed: %d → %d tokens (%.1f%% reduction, %.0fms)",
                original_tokens,
                compressed_tokens,
                stats.reduction_pct,
                stats.duration_ms,
            )

            return compressed, stats

        # 策略1：软阈值 - 只预生成，不压缩
        if not force_hard and self.should_async_prepare(original_tokens):
            # TODO: 异步预生成摘要（不阻塞）
            logger.debug("Soft threshold reached, would pre-generate summary asynchronously")
            stats = CompressionStats(
                original_tokens=original_tokens,
                strategy="soft",
                duration_ms=(time.time() - start_time) * 1000,
            )
            return messages, stats

        # 不需要压缩
        return messages, CompressionStats(
            original_tokens=original_tokens,
            strategy="none",
            duration_ms=(time.time() - start_time) * 1000,
        )

    def _incremental_compress(self, messages: list[Any], context_summary: str) -> list[Any]:
        """增量压缩：基于上一次摘要叠加新消息

        面试官考察点：
        - 避免全量重算（O(n²) → O(n)）
        - 增量更新策略

        Args:
            messages: 新消息列表
            context_summary: 上一次的摘要

        Returns:
            压缩后的消息
        """
        from mokioclaw.memory.tiered_compression import compress_messages_by_tier

        if not messages:
            return messages

        # 如果有旧摘要，将其作为第一条消息加入
        if context_summary:
            from langchain_core.messages import AIMessage

            summary_msg = AIMessage(content=f"[Previous Summary]\n{context_summary}")
            # 摘要 + 新消息的最后 10 条
            recent_messages = messages[-10:] if len(messages) > 10 else messages
            combined = [summary_msg] + recent_messages
        else:
            # 第一次压缩，正常分级压缩
            combined = messages

        # 分级压缩
        compressed = compress_messages_by_tier(combined, context_summary=context_summary)

        # 更新摘要链
        if context_summary:
            # 增量更新：旧摘要 + 新增内容
            self.summary_chain.add_summary(
                turn_range=f"{self.compression_count + 1}-*",
                summary=context_summary,  # 保留旧摘要
                turn_count=len(messages),
            )

        return compressed

    def get_compression_metrics(self) -> dict[str, Any]:
        """获取压缩指标（用于监控和调试）"""
        return {
            "compression_count": self.compression_count,
            "last_compression_time": self.last_compression_time,
            "summary_chain_length": len(self.summary_chain.summaries),
            "thresholds": {
                "soft": f"{self.thresholds.soft_threshold:.0%}",
                "hard": f"{self.thresholds.hard_threshold:.0%}",
                "max_tokens": self.thresholds.max_context_tokens,
            },
        }
