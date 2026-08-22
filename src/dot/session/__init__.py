"""
dot.session — 会话域

  - session:     Session 数据结构（State IS Session）
  - persistence: session.json / turn 快照 / rewind / agent 专用 git
  - manager:     SessionManager（单会话管理 + 图驱动 + 自定义介入恢复）
"""
from __future__ import annotations

from .session import MAX_ATTEMPT_DEFAULT, REPLAN_THRESHOLD, Session
from .persistence import SessionPersistence, persist_turn
from .manager import SessionManager

__all__ = [
    "Session",
    "REPLAN_THRESHOLD",
    "MAX_ATTEMPT_DEFAULT",
    "SessionPersistence",
    "persist_turn",
    "SessionManager",
]
