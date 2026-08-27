"""
dot.coding.compress.planner — 压缩规划器

根据上下文窗口使用率决定压缩级别。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from dot.ai.limits import ContextWindowInfo


class CompactionLevel(Enum):
    NONE = "none"
    L1 = "l1"  # ≥50%: 去掉可恢复 tool 结果
    L2 = "l2"  # ≥70%: 删除老旧 tool 调用
    L3 = "l3"  # ≥85%: LLM 生成结构化摘要


@dataclass
class CompactionPlan:
    """压缩计划"""
    level: CompactionLevel
    context_info: ContextWindowInfo
    reason: str = ""


def plan_compaction(context_info: ContextWindowInfo) -> CompactionPlan:
    """根据上下文使用率规划压缩级别"""
    if context_info.should_compact_l3:
        return CompactionPlan(
            level=CompactionLevel.L3,
            context_info=context_info,
            reason=f"Usage {context_info.usage_ratio:.0%} >= 85%, need LLM summary",
        )
    if context_info.should_compact_l2:
        return CompactionPlan(
            level=CompactionLevel.L2,
            context_info=context_info,
            reason=f"Usage {context_info.usage_ratio:.0%} >= 70%, remove old tool calls",
        )
    if context_info.should_compact_l1:
        return CompactionPlan(
            level=CompactionLevel.L1,
            context_info=context_info,
            reason=f"Usage {context_info.usage_ratio:.0%} >= 50%, remove recoverable results",
        )
    return CompactionPlan(
        level=CompactionLevel.NONE,
        context_info=context_info,
        reason=f"Usage {context_info.usage_ratio:.0%} below threshold",
    )
