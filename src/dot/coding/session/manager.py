"""
dot.coding.session.manager — SessionManager 会话生命周期管理

负责会话的创建、恢复、切换、分支。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from .session import Session, SessionConfig
from .storage import SessionStorage

logger = logging.getLogger(__name__)


class SessionManager:
    """会话生命周期管理"""

    def __init__(
        self,
        sessions_root: Path,
        workspace: Path,
    ) -> None:
        self._root = sessions_root
        self._workspace = workspace
        self._storage = SessionStorage(sessions_root)
        self._active: Session | None = None

    @property
    def session(self) -> Session | None:
        return self._active

    def get_or_create(self, session_id: str | None = None) -> Session:
        """获取或创建会话"""
        if session_id and self._storage.exists(session_id):
            return self.restore(session_id)
        return self.create(session_id)

    def create(self, session_id: str | None = None) -> Session:
        """创建新会话"""
        sid = session_id or str(uuid.uuid4())[:8]
        session = Session(
            session_id=sid,
            workspace=self._workspace,
        )
        self._active = session
        self._storage.save(session)
        logger.info("[session] Created: %s", sid)
        return session

    def restore(self, session_id: str) -> Session:
        """恢复历史会话"""
        data = self._storage.load(session_id)
        if data is None:
            logger.warning("[session] Not found: %s, creating new", session_id)
            return self.create(session_id)
        session = Session(
            session_id=session_id,
            workspace=self._workspace,
        )
        self._active = session
        logger.info("[session] Restored: %s", session_id)
        return session

    def switch_to(self, session_id: str) -> Session:
        """切换活跃会话"""
        return self.restore(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话"""
        return self._storage.list_all()
