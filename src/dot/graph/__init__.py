"""
dot.graph — LangGraph 图编排

  - coding_graph: 六节点图（compress/plan/coding/valid/intervene/finally）
  - prompts:      节点 system prompt 模板
"""
from __future__ import annotations

from ..compress.node import context_compress_node
from .coding_graph import DotAgentState, build_graph, compile_graph
from .prompts import (
    get_coding_system_prompt,
    get_plan_system_prompt,
    get_valid_system_prompt,
)

__all__ = [
    "context_compress_node",
    "DotAgentState",
    "build_graph",
    "compile_graph",
    "get_plan_system_prompt",
    "get_coding_system_prompt",
    "get_valid_system_prompt",
]
