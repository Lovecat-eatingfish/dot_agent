"""
dot.coding.compress — 三级上下文压缩

  - L1（≥50%）：去掉可恢复的 tool 结果（read_file），截取少量内容 + 路径
  - L2（≥70%）：删除老旧的 tool 调用（bash/grep 等输出）
  - L3（≥85%）：LLM 生成结构化摘要，替换旧消息

自动接入：AutoCompactor 实现 agent 层的 CompactionGate 契约，
由 host 注入 harness，在每轮 turn 边界检查并触发。
"""
from __future__ import annotations

from .planner import CompactionLevel, CompactionPlan, plan_compaction
from .l1_extract import compact_l1
from .l2_summarize import compact_l2
from .l3_semantic import compact_l3
from .compactor import CompactionOutcome, ContextCompactor
from .auto_compact import AutoCompactor

__all__ = [
    "CompactionLevel",
    "CompactionPlan",
    "plan_compaction",
    "compact_l1",
    "compact_l2",
    "compact_l3",
    "CompactionOutcome",
    "ContextCompactor",
    "AutoCompactor",
]
