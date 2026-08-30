"""
dot.coding.session — 会话管理

  - Session: 消息历史 + 文件快照 + 配置
  - SessionManager: 生命周期（create / restore / switch / resume）+ turn 落盘与回滚
  - SessionStorage: 增量 append-only JSONL 持久化
  - SessionGit: 每会话独立 git 快照（workspace 文件按 turn 回滚）
"""
from __future__ import annotations

from .session import Session, SessionConfig, FileSnapshot, TurnRecord
from .manager import SessionManager
from .storage import SessionStorage
from .git import SessionGit

__all__ = [
    "Session",
    "SessionConfig",
    "FileSnapshot",
    "TurnRecord",
    "SessionManager",
    "SessionStorage",
    "SessionGit",
]
