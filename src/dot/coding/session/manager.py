"""
dot.coding.session.manager — SessionManager 会话生命周期管理

负责会话的创建、恢复、切换、分支。
每轮对话通过 commit_turn 增量落盘（消息 + git 快照 hash），
支持按 turn_id 回滚（/rewind）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .git import SessionGit
from .session import Session, SessionConfig, TurnRecord
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
        self._git: SessionGit | None = None

    @property
    def session_storage(self) -> SessionStorage:
        return self._storage

    @property
    def session(self) -> Session | None:
        return self._active

    @property
    def git(self) -> SessionGit | None:
        return self._git

    def get_or_create(self, session_id: str | None = None) -> Session:
        """获取或创建会话"""
        if session_id and self._storage.exists(session_id):
            return self.restore(session_id)
        return self.create(session_id)

    def create(self, session_id: str | None = None) -> Session:
        """创建新会话（写 meta 行 + git 基线 commit）"""
        sid = session_id or self._generate_session_id()
        session = Session(
            session_id=sid,
            workspace=self._workspace,
        )
        self._active = session
        self._attach_git(sid)
        self._storage.append_meta(sid, {
            "session_id": sid,
            "agent_mode": session.config.agent_mode,
            "workspace": str(self._workspace),
            "created_at": self._now(),
        })
        logger.info("[session] Created: %s", sid)
        return session

    def restore(self, session_id: str) -> Session:
        """恢复历史会话（增量格式 / 旧整快照格式均兼容）"""
        data = self._storage.read_full(session_id)
        if not data["meta"] and not data["entries"] and not data["turns"]:
            logger.warning("[session] Not found: %s, creating new", session_id)
            return self.create(session_id)
        session = Session(
            session_id=session_id,
            workspace=self._workspace,
        )
        try:
            session.messages = self._deserialize(data["entries"])
            session.turns = [
                TurnRecord(
                    turn_id=t["turn_id"], msg_count_end=t["msg_count_end"],
                    commit=t["commit"], timestamp=t["timestamp"],
                )
                for t in data["turns"]
            ]
            session._persisted_count = len(session.messages)
            mode = (data["meta"] or {}).get("agent_mode")
            if mode:
                session.config.agent_mode = mode
        except Exception:
            logger.exception("[session] Failed to restore %s, creating new", session_id)
            return self.create(session_id)
        session.workspace = self._workspace
        self._active = session
        self._attach_git(session_id)
        logger.info("[session] Restored: %s (%d messages)", session_id, len(session.messages))
        return session

    @staticmethod
    def _deserialize(entries: list[dict[str, Any]]) -> list[Any]:
        """把逐条消息 dict 反序列化为 AgentMessage（复用 Session.from_snapshot 的适配器）"""
        from .session import deserialize_messages
        return deserialize_messages([e["message"] for e in entries])

    def switch_to(self, session_id: str) -> Session:
        """切换活跃会话"""
        return self.restore(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话"""
        return self._storage.list_all()

    # ============================================================
    # Turn 级落盘与回滚
    # ============================================================

    def commit_turn(self, new_messages: list[Any]) -> int:
        """一轮对话结束：git 快照 + 增量落盘新消息 + 写 turn_end 行

        返回本轮 turn_id。git 不可用时 commit 为空串（消息照常落盘）。
        """
        session = self._active
        if session is None:
            raise RuntimeError("No active session")
        turn_id = session.turns[-1].turn_id + 1 if session.turns else 1

        commit = ""
        if self._git is not None and self._git.available:
            try:
                commit = self._git.commit_turn(turn_id)
            except Exception as exc:
                logger.warning("[session] git commit failed (turn %d): %s", turn_id, exc)

        self._storage.append_messages(
            session.session_id, turn_id,
            [m.model_dump() for m in new_messages],
        )
        self._storage.append_turn_end(
            session.session_id, turn_id,
            msg_count_end=len(session.messages), commit=commit, timestamp=self._now(),
        )
        session.turns.append(TurnRecord(
            turn_id=turn_id, msg_count_end=len(session.messages),
            commit=commit, timestamp=self._now(),
        ))
        session._persisted_count = len(session.messages)
        logger.info("[session] Turn %d committed (%d new msgs, git=%s)",
                    turn_id, len(new_messages), commit[:8] or "n/a")
        return turn_id

    def rewind(self, turn_id: int) -> str:
        """回滚到指定轮次结束时的状态（消息 + workspace 文件）

        返回该轮的 git commit（可能为空串）。后续轮次从 session 截断丢弃。
        """
        session = self._active
        if session is None:
            raise RuntimeError("No active session")
        target = next((t for t in session.turns if t.turn_id == turn_id), None)
        if target is None:
            raise ValueError(f"Unknown turn_id: {turn_id}")

        session.messages = session.messages[:target.msg_count_end]
        session.turns = [t for t in session.turns if t.turn_id <= turn_id]
        session._persisted_count = len(session.messages)

        # 重写文件：meta + 保留的消息行 + 保留的轮次行
        full = self._storage.read_full(session.session_id)
        kept_turn_ids = {t.turn_id for t in session.turns}
        kept_entries = [
            e for e in full["entries"]
            if e["turn_id"] is None or e["turn_id"] in kept_turn_ids
        ]
        self._storage.rewrite(
            session.session_id,
            meta={k: v for k, v in (full["meta"] or {}).items() if k != "type"},
            entries=kept_entries,
            turns=[
                {"turn_id": t.turn_id, "msg_count_end": t.msg_count_end,
                 "commit": t.commit, "timestamp": t.timestamp}
                for t in session.turns
            ],
        )

        if target.commit and self._git is not None and self._git.available:
            self._git.restore(target.commit)
        logger.info("[session] Rewound to turn %d (git=%s)", turn_id, target.commit[:8] or "n/a")
        return target.commit

    # ============================================================
    # 内部
    # ============================================================

    def _attach_git(self, session_id: str) -> None:
        """为当前会话挂载 git 快照管理器（初始化失败则降级为无快照）"""
        git = SessionGit(self._storage.session_dir(session_id), self._workspace)
        try:
            git.init()
            self._git = git
        except Exception as exc:
            logger.warning("[session] git snapshot disabled for %s: %s", session_id, exc)
            self._git = git  # 保留对象但 available=False，commit_turn 会跳过
            git._available = False

    def _generate_session_id(self) -> str:
        """生成会话 ID：年月日-时分秒（同一秒内冲突则递增序号）"""
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        sid = base
        seq = 0
        while self._storage.exists(sid):
            seq += 1
            sid = f"{base}-{seq}"
        return sid

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
