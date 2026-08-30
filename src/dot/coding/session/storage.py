"""
dot.coding.session.storage — SessionStorage 持久化层

增量 append-only JSONL，支持按 turn 回滚（/rewind）。

文件格式（每行一个 JSON 对象）：
  {"type": "meta", "session_id": ..., "agent_mode": ..., "workspace": ..., "created_at": ...}
  {"type": "msg", "turn_id": N, "message": {...}}            # 逐条消息，turn_id 归属轮次
  {"type": "turn_end", "turn_id": N, "msg_count_end": M, "commit": "<git hash>", "timestamp": ...}

兼容旧格式：无 "type" 字段的行 = 旧版整快照（messages 全量列表），
读取时取最后一条旧快照作为消息基线（无轮次信息）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionStorage:
    """会话持久化层（增量 append-only JSONL）"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.jsonl"

    def session_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id)

    def exists(self, session_id: str) -> bool:
        return self._session_file(session_id).is_file()

    # ============================================================
    # 写入
    # ============================================================

    def append_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        self._append(session_id, {"type": "meta", **meta})

    def append_messages(
        self, session_id: str, turn_id: int, messages: list[dict[str, Any]],
    ) -> None:
        """追加一个 turn 的新增消息（逐条一行）"""
        for message in messages:
            self._append(session_id, {"type": "msg", "turn_id": turn_id, "message": message})

    def append_turn_end(
        self, session_id: str, turn_id: int, msg_count_end: int,
        commit: str = "", timestamp: str = "",
    ) -> None:
        self._append(session_id, {
            "type": "turn_end",
            "turn_id": turn_id,
            "msg_count_end": msg_count_end,
            "commit": commit,
            "timestamp": timestamp,
        })

    def rewrite(
        self,
        session_id: str,
        meta: dict[str, Any] | None,
        entries: list[dict[str, Any]],
        turns: list[dict[str, Any]],
    ) -> None:
        """整体重写 session 文件（/rewind 截断时使用）"""
        lines: list[str] = []
        if meta:
            lines.append(json.dumps({"type": "meta", **meta}, ensure_ascii=False))
        for entry in entries:
            lines.append(json.dumps({"type": "msg", **entry}, ensure_ascii=False))
        for turn in turns:
            lines.append(json.dumps({"type": "turn_end", **turn}, ensure_ascii=False))
        path = self._session_file(session_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        except OSError as exc:
            logger.error("[storage] Failed to rewrite %s: %s", session_id, exc)

    def _append(self, session_id: str, record: dict[str, Any]) -> None:
        """追加写入 JSONL"""
        path = self._session_file(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("[storage] Failed to write %s: %s", session_id, exc)

    # ============================================================
    # 读取
    # ============================================================

    def read_full(self, session_id: str) -> dict[str, Any]:
        """全量读取并解析：返回 meta、逐条消息（带 turn_id）、轮次记录

        兼容旧格式（整快照行）：取最后一条旧快照作为消息基线，无轮次。
        """
        meta: dict[str, Any] | None = None
        entries: list[dict[str, Any]] = []       # {"turn_id": int|None, "message": {...}}
        turns: list[dict[str, Any]] = []
        legacy_messages: list[dict[str, Any]] | None = None

        path = self._session_file(session_id)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return {"meta": None, "entries": [], "turns": []}

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[storage] Skip corrupt line in %s", session_id)
                continue

            record_type = record.get("type")
            if record_type == "meta":
                meta = record
            elif record_type == "msg":
                entries.append({"turn_id": record.get("turn_id"), "message": record["message"]})
            elif record_type == "turn_end":
                turns.append({
                    "turn_id": record["turn_id"],
                    "msg_count_end": record["msg_count_end"],
                    "commit": record.get("commit", ""),
                    "timestamp": record.get("timestamp", ""),
                })
            elif "messages" in record:
                # 旧格式整快照：消息基线覆盖，轮次清空（旧快照无轮次）
                legacy_messages = record["messages"]
                turns = []
                entries = []

        if legacy_messages is not None:
            entries = [{"turn_id": None, "message": m} for m in legacy_messages]

        return {"meta": meta, "entries": entries, "turns": turns}

    # 兼容旧接口：最后一条记录（旧格式快照或 turn_end）
    def load(self, session_id: str) -> dict[str, Any] | None:
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
        """列出所有会话（新格式取 meta + 消息数；旧格式取最后快照）"""
        sessions = []
        for d in self._root.iterdir():
            if d.is_dir() and (d / "session.jsonl").is_file():
                data = self.read_full(d.name)
                if data["meta"] or data["entries"]:
                    sessions.append({
                        "session_id": d.name,
                        "message_count": len(data["entries"]),
                        "workspace": (data["meta"] or {}).get("workspace", "?"),
                    })
        return sessions
