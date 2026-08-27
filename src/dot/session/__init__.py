"""
dot.session — 会话域

  - session:        Session 数据结构（State IS Session）
  - turn_state:     TurnState（per-turn 执行状态）
  - agent_context:  AgentContext（进程级组件容器）
  - persistence:    session.json / turn 快照 / rewind / agent 专用 git
  - manager:        SessionManager（单会话管理 + 图驱动 + 自定义介入恢复）
"""
from __future__ import annotations

from .session import MAX_ATTEMPT_DEFAULT, REPLAN_THRESHOLD, Session
from .turn_state import TurnState
from .agent_context import AgentContext
from .persistence import SessionPersistence, persist_turn
from .manager import SessionManager

__all__ = [
    "Session",
    "TurnState",
    "AgentContext",
    "REPLAN_THRESHOLD",
    "MAX_ATTEMPT_DEFAULT",
    "SessionPersistence",
    "persist_turn",
    "SessionManager",
]
