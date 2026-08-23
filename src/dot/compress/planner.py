"""
CompressionPlanner — 压缩策略决策

根据当前 token 数和历史状态，决定触发哪一级压缩。
设计约束（对齐设计文档）：
  - L1：轻量提取，压缩后立即执行
  - L2：滑窗摘要，每 N 轮触发（N=10）
  - L3：语义压缩，接近窗口限时触发
  - L2/L3 可同时触发（先 L2 再 L3）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.log import get_logger
from .budget import ContextBudgetAllocator
from .state import CompressionState

logger = get_logger(__name__)

# L2 最小触发间隔（轮数）
L2_MIN_INTERVAL = 10


@dataclass
class CompressionDecision:
    """压缩决策结果"""
    level: str              # "NONE" | "L1" | "L2" | "L3" | "L2+L3"
    reason: str             # 决策原因
    current_tokens: int     # 当前 token 数
    threshold: int          # 触发阈值

    @property
    def needs_compression(self) -> bool:
        return self.level != "NONE"

    @property
    def needs_l1(self) -> bool:
        return "L1" in self.level or "L2" in self.level or "L3" in self.level

    @property
    def needs_l2(self) -> bool:
        return "L2" in self.level

    @property
    def needs_l3(self) -> bool:
        return "L3" in self.level


class CompressionPlanner:
    """压缩策略决策器

    根据当前 token 数、context window、历史压缩状态，
    决定触发哪一级压缩。
    """

    def __init__(
        self,
        budget_allocator: ContextBudgetAllocator,
        compression_state: CompressionState,
        current_turn: int = 0,
    ) -> None:
        self.budget = budget_allocator
        self.state = compression_state
        self.current_turn = current_turn

    def decide(self, current_tokens: int) -> CompressionDecision:
        """决定压缩级别

        决策逻辑：
        1. < compression_threshold → NONE
        2. ≥ l1_threshold 且 < l2_threshold → L1
        3. ≥ l2_threshold 且距上次 L2 ≥ 10 轮 → L2
        4. ≥ l3_threshold → L3
        5. L2 + L3 可同时触发
        """
        if current_tokens < self.budget.compression_threshold:
            return CompressionDecision(
                level="NONE",
                reason=f"tokens ({current_tokens}) < threshold ({self.budget.compression_threshold})",
                current_tokens=current_tokens,
                threshold=self.budget.compression_threshold,
            )

        needs_l2 = False
        needs_l3 = False
        reasons = []

        # L2 判断：≥ l2_threshold 且距上次 L2 ≥ L2_MIN_INTERVAL 轮
        if current_tokens >= self.budget.l2_threshold:
            turns_since_l2 = self.state.turns_since_l2(self.current_turn)
            if turns_since_l2 >= L2_MIN_INTERVAL:
                needs_l2 = True
                reasons.append(f"L2: tokens ({current_tokens}) >= l2_threshold ({self.budget.l2_threshold}), {turns_since_l2} turns since last L2")
            else:
                reasons.append(f"L2 skipped: only {turns_since_l2} turns since last L2 (need {L2_MIN_INTERVAL})")

        # L3 判断：≥ l3_threshold
        if current_tokens >= self.budget.l3_threshold:
            needs_l3 = True
            reasons.append(f"L3: tokens ({current_tokens}) >= l3_threshold ({self.budget.l3_threshold})")

        # 组合决策
        if needs_l2 and needs_l3:
            level = "L2+L3"
        elif needs_l2:
            level = "L2"
        elif needs_l3:
            level = "L3"
        else:
            # 仅 L1
            level = "L1"
            reasons.append(f"L1: tokens ({current_tokens}) >= l1_threshold ({self.budget.l1_threshold})")

        return CompressionDecision(
            level=level,
            reason="; ".join(reasons),
            current_tokens=current_tokens,
            threshold=self.budget.compression_threshold,
        )
