"""
dot.rag.store — 元数据存储抽象层

两类数据：
  - documents：文档登记（版本、chunk 数、逻辑删除标记）
  - traces：每次 RAG 查询的完整链路（检索耗时、生成耗时、召回片段）

接口为 MetadataStoreBase 抽象类，默认实现 SqliteMetadataStore；
通过配置 RAG_METADATA_BACKEND（工厂 create_metadata_store）切换后端，
后续可对接 MySQL / PostgreSQL / MongoDB 等，pipeline 代码零改动。

向量数据在向量库（Chroma 等），业务元数据在这里，互不替代。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import RagSettings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_filename ON documents(filename);

CREATE TABLE IF NOT EXISTS traces (
    trace_id   TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL DEFAULT '',
    steps      TEXT NOT NULL DEFAULT '[]',
    top_k      INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,             -- user / assistant
    content    TEXT NOT NULL,
    trace_id   TEXT,
    sources    TEXT NOT NULL DEFAULT '[]', -- JSON 数组（assistant 消息的来源文件）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class DocumentRecord:
    id: str
    filename: str
    path: str
    version: int = 1
    chunk_count: int = 0
    status: str = "active"  # active / deleted
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TraceStep:
    """链路中的一个步骤（span，支持父子嵌套形成 span 树）

    start_ms 为相对本次查询开始的偏移；children 递归嵌套，
    前端按树渲染瀑布图。
    """

    stage: str
    cost_ms: float = 0.0
    start_ms: float = 0.0
    detail: dict = field(default_factory=dict)
    children: list["TraceStep"] = field(default_factory=list)

    def add_child(self, child: "TraceStep") -> "TraceStep":
        self.children.append(child)
        return child


@dataclass
class TraceRecord:
    trace_id: str
    question: str
    answer: str = ""
    steps: list[TraceStep] = field(default_factory=list)
    top_k: int = 0
    chunk_count: int = 0
    status: str = "done"  # done / error
    created_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class SessionRecord:
    id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class MessageRecord:
    id: str
    session_id: str
    role: str  # user / assistant
    content: str
    trace_id: str = ""
    sources: list[str] = field(default_factory=list)
    created_at: str = ""


class MetadataStoreBase(ABC):
    """元数据存储抽象接口

    实现方只需保证 DocumentRecord / TraceRecord 的语义不变；
    ID 生成统一走基类，保证各后端格式一致。
    """

    @staticmethod
    def new_doc_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:16]

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def new_message_id() -> str:
        return uuid.uuid4().hex[:16]

    # ---------- 文档 ----------

    @abstractmethod
    def upsert_document(self, doc: DocumentRecord) -> DocumentRecord:
        """按文件名登记文档：同名文件重新入库时版本号累计 +1"""

    @abstractmethod
    def get_document(self, doc_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    def get_active_by_filename(self, filename: str) -> DocumentRecord | None: ...

    @abstractmethod
    def list_documents(self, include_deleted: bool = False) -> list[DocumentRecord]: ...

    @abstractmethod
    def soft_delete_document(self, doc_id: str) -> bool: ...

    # ---------- trace ----------

    @abstractmethod
    def insert_trace(self, trace: TraceRecord) -> None: ...

    @abstractmethod
    def list_traces(self, limit: int = 50) -> list[dict]: ...

    @abstractmethod
    def get_trace(self, trace_id: str) -> dict | None: ...

    # ---------- 会话（聊天多轮） ----------

    @abstractmethod
    def create_session(self, title: str = "") -> SessionRecord: ...

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRecord | None: ...

    @abstractmethod
    def list_sessions(self, limit: int = 100) -> list[SessionRecord]: ...

    @abstractmethod
    def update_session_title(self, session_id: str, title: str) -> bool: ...

    @abstractmethod
    def delete_session(self, session_id: str) -> bool: ...

    @abstractmethod
    def append_message(self, message: MessageRecord) -> MessageRecord: ...

    @abstractmethod
    def list_messages(self, session_id: str, limit: int = 200) -> list[MessageRecord]: ...


class SqliteMetadataStore(MetadataStoreBase):
    """SQLite 实现（线程安全，WAL 模式）"""

    def __init__(self, settings: RagSettings | None = None):
        self.settings = settings or RagSettings()
        db_path = Path(self.settings.metadata_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------- documents ----------

    def upsert_document(self, doc: DocumentRecord) -> DocumentRecord:
        """按文件名登记文档：同名文件重新入库时版本号累计 +1（含已删除的记录）"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE filename = ? ORDER BY version DESC LIMIT 1",
                (doc.filename,),
            ).fetchone()
            if row:
                doc.id = doc.id or self.new_doc_id()
                doc.version = row["version"] + 1
            else:
                doc.id = doc.id or self.new_doc_id()
                doc.version = 1
                doc.created_at = _now()
            doc.updated_at = _now()
            self._conn.execute(
                "INSERT INTO documents (id, filename, path, version, chunk_count, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path, version=excluded.version, "
                "chunk_count=excluded.chunk_count, updated_at=excluded.updated_at, status='active'",
                (doc.id, doc.filename, doc.path, doc.version, doc.chunk_count, doc.created_at, doc.updated_at),
            )
            self._conn.commit()
        return doc

    def get_document(self, doc_id: str) -> DocumentRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return DocumentRecord(**dict(row)) if row else None

    def get_active_by_filename(self, filename: str) -> DocumentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE filename = ? AND status = 'active' LIMIT 1", (filename,)
            ).fetchone()
        return DocumentRecord(**dict(row)) if row else None

    def list_documents(self, include_deleted: bool = False) -> list[DocumentRecord]:
        sql = "SELECT * FROM documents" + ("" if include_deleted else " WHERE status = 'active'")
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at DESC").fetchall()
        return [DocumentRecord(**dict(r)) for r in rows]

    def soft_delete_document(self, doc_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE documents SET status = 'deleted', updated_at = ? WHERE id = ?", (_now(), doc_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ---------- traces ----------

    def insert_trace(self, trace: TraceRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO traces (trace_id, question, answer, steps, top_k, chunk_count, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace.trace_id,
                    trace.question,
                    trace.answer,
                    json.dumps([asdict(s) for s in trace.steps], ensure_ascii=False),
                    trace.top_k,
                    trace.chunk_count,
                    trace.status,
                    _now(),
                ),
            )
            self._conn.commit()

    def list_traces(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM traces ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._trace_row_to_dict(r, with_answer=True) for r in rows]

    def get_trace(self, trace_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        return self._trace_row_to_dict(row, with_answer=True) if row else None

    @staticmethod
    def _trace_row_to_dict(row: sqlite3.Row, with_answer: bool = False) -> dict:
        d = dict(row)
        d["steps"] = json.loads(d.get("steps") or "[]")
        if not with_answer:
            d.pop("answer", None)
        return d

    # ---------- 会话（聊天多轮） ----------

    def create_session(self, title: str = "") -> SessionRecord:
        record = SessionRecord(id=self.new_session_id(), title=title, created_at=_now(), updated_at=_now())
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (record.id, record.title, record.created_at, record.updated_at),
            )
            self._conn.commit()
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return SessionRecord(**dict(row)) if row else None

    def list_sessions(self, limit: int = 100) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [SessionRecord(**dict(r)) for r in rows]

    def update_session_title(self, session_id: str, title: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, _now(), session_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cur2 = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()
        return cur2.rowcount > 0

    def append_message(self, message: MessageRecord) -> MessageRecord:
        if not message.created_at:
            message.created_at = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, session_id, role, content, trace_id, sources, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.session_id,
                    message.role,
                    message.content,
                    message.trace_id,
                    json.dumps(message.sources, ensure_ascii=False),
                    message.created_at,
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), message.session_id)
            )
            self._conn.commit()
        return message

    def list_messages(self, session_id: str, limit: int = 200) -> list[MessageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        records = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d.get("sources") or "[]")
            records.append(MessageRecord(**d))
        return records

    def close(self) -> None:
        self._conn.close()


# ---------- 工厂 ----------

_BACKENDS: dict[str, type[MetadataStoreBase]] = {
    "sqlite": SqliteMetadataStore,
}


def create_metadata_store(settings: RagSettings | None = None) -> MetadataStoreBase:
    """按配置创建元数据存储后端（RAG_METADATA_BACKEND）"""
    settings = settings or RagSettings()
    backend = settings.metadata_backend.lower()
    if backend not in _BACKENDS:
        raise ValueError(f"未知的元数据后端: {backend}，可选: {sorted(_BACKENDS)}")
    return _BACKENDS[backend](settings)
