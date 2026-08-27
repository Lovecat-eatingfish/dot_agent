"""
路由函数 — 从 coding_graph.py 拆分

职责：
  - route_coding_agent: coding_agent 之后的路由
  - route_valid_node: valid_node 之后的路由
"""
from __future__ import annotations

from typing import Any


def route_coding_agent(state: Any) -> str:
    """coding_agent 之后：
    - need_human_intervene → human_intervene
    - plan_invalid（且未达阈值）→ plan_node（replan）
    - 其他 → valid_node
    """
    session = state["session"]
    if session.need_human_intervene():
        return "human_intervene"
    if session.is_plan_invalid():
        return "plan_node"
    return "valid_node"


def route_valid_node(state: Any) -> str:
    """valid_node 之后：
    - passed → finally_node
    - failed 且 attempt < max → coding_agent（重试）
    - failed 且 attempt >= max → human_intervene
    """
    session = state["session"]
    if session.get_validate_result().get("passed"):
        return "finally_node"

    if session.get_attempt_count() < session.max_attempt:
        return "coding_agent"
    return "human_intervene"
