"""
dot.coding.session.storage — SessionStorage 持久化层

append-only JSONL 存储，支持树形分支。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionStorage:
    """会话持久化层（append-only JSONL）"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.jsonl"

    def exists(self, session_id: str) -> bool:
        return self._session_file(session_id).is_file()

    def save(self, session: Any) -> None:
        """保存会话（追加写入）"""
        sid = session.session_id
        session_dir = self._session_dir(sid)
        session_dir.mkdir(parents=True, exist_ok=True)

        snapshot = session.to_snapshot() if hasattr(session, "to_snapshot") else {"session_id": sid}
        self._append(sid, snapshot)

    def load(self, session_id: str) -> dict[str, Any] | None:
        """加载会话（读取最后一条记录）"""
        path = self._session_file(session_id)
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            if not lines:
                return None
            return json.loads(lines[-1])
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[storage] Failed to load %s: %s", session_id, exc)
            return None

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有会话"""
        sessions = []
        for d in self._root.iterdir():
            if d.is_dir() and (d / "session.jsonl").is_file():
                data = self.load(d.name)
                if data:
                    sessions.append(data)
        return sessions

    def _append(self, session_id: str, record: dict[str, Any]) -> None:
        """追加写入 JSONL"""
        path = self._session_file(session_id)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("[storage] Failed to write %s: %s", session_id, exc)
