"""
dot.compress — 三级动态上下文压缩

设计（对齐 doc/Agent 三级动态上下文压缩最终设计文档）：
  - L1（关键事实提取）：压缩后立即执行，从被裁剪消息中提取关键事实
  - L2（滑窗摘要）：每 N 轮触发，对旧消息生成摘要替换原始消息
  - L3（语义压缩）：接近窗口限时触发，按重要性评分丢弃低价值消息

核心组件：
  - ContextBudgetAllocator: token 预算计算
  - CompressionPlanner: 压缩策略决策
  - CompressionState: 压缩状态管理
  - context_compress_node: 图节点入口

用法：
    from dot.compress import context_compress_node, CompressionState
"""
from __future__ import annotations

from .budget import ContextBudgetAllocator
from .node import context_compress_node
from .planner import CompressionDecision, CompressionPlanner
from .state import CompressionHistoryEntry, CompressionState

__all__ = [
    "context_compress_node",
    "ContextBudgetAllocator",
    "CompressionPlanner",
    "CompressionDecision",
    "CompressionState",
    "CompressionHistoryEntry",
]
