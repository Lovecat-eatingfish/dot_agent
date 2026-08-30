"""
dot.coding.session.session — Session 会话数据

Session 只持有消息历史和 Harness 引用，不持有进程级组件。
消息使用纯 dict（JSON 可序列化），不依赖 langchain BaseMessage。
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from dot.ai.types import AgentMessage

_message_list_adapter: TypeAdapter[list[AgentMessage]] = TypeAdapter(list[AgentMessage])


@dataclass
class FileSnapshot:
    """文件读取快照（用于写入保护）"""
    path: Path
    mtime_ns: int
    content_hash: str
    complete: bool


@dataclass
class SessionConfig:
    """会话配置"""
    agent_mode: str = "auto"
    max_turns: int | None = None
    max_replan: int = 3


@dataclass
class Session:
    """会话数据

    职责：身份 + 消息历史 + 文件快照 + 配置
    不持有：进程级组件（MCP/Skill/Hook/Permission/Tracer）
    """
    session_id: str
    workspace: Path = field(default_factory=Path.cwd)
    messages: list[AgentMessage] = field(default_factory=list)
    config: SessionConfig = field(default_factory=SessionConfig)

    # 文件快照（写入保护）
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)

    # 消息序号（线程安全）
    _message_seq: int = 0
    _message_seq_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def record_read(self, path: Path, *, complete: bool, content: bytes | None = None) -> None:
        """记录文件读取快照"""
        resolved = path.resolve()
        if content is None:
            content = resolved.read_bytes()
        stat = resolved.stat()
        content_hash = hashlib.sha256(content).hexdigest()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            content_hash=content_hash,
            complete=complete,
        )

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        return self.read_files.get(path.resolve())

    def is_file_modified(self, path: Path) -> bool:
        snap = self.snapshot_for(path)
        if snap is None:
            return True
        resolved = path.resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return True
        if snap.mtime_ns == stat.st_mtime_ns:
            return False
        try:
            current_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return True
        return snap.content_hash != current_hash

    def next_message_id(self) -> str:
        with self._message_seq_lock:
            self._message_seq += 1
            return f"msg-{self._message_seq:05d}"

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为快照字典"""
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "message_count": len(self.messages),
            "agent_mode": self.config.agent_mode,
            "messages": [m.model_dump() for m in self.messages],
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> Session:
        """从快照字典反序列化（含消息历史）"""
        session = cls(
            session_id=data["session_id"],
            workspace=Path(data.get("workspace", Path.cwd())),
        )
        mode = data.get("agent_mode")
        if mode:
            session.config.agent_mode = mode
        raw_messages = data.get("messages") or []
        session.messages = _message_list_adapter.validate_python(raw_messages)
        session._message_seq = len(session.messages)
        return session
