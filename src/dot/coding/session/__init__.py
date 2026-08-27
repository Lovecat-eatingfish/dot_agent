"""
dot.coding.session — 会话管理

职责分离：
  - Session: 消息历史 + 文件快照 + 配置
  - SessionManager: 生命周期（create / restore / switch / branch）
  - SessionStorage: 持久化层（append-only JSONL）
  - SessionTree: 分支管理（parent_id 链接）
"""
from __future__ import annotations

from .session import Session, SessionConfig, FileSnapshot
from .manager import SessionManager
from .storage import SessionStorage
from .tree import SessionTree

__all__ = [
    "Session",
    "SessionConfig",
    "FileSnapshot",
    "SessionManager",
    "SessionStorage",
    "SessionTree",
]
