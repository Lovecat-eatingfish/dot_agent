"""
dot.coding.review.persistence — 变更台账持久化（每会话一份）

让 pending 评审状态在会话恢复（/resume、agent tui -r）后仍然可用：
- 记录本体   ：<session_dir>/review-ledger.json
- 内容快照   ：<session_dir>/review-snapshots/<id>.txt（回滚所需 before_text）
  fallback_git 记录没有快照文件，恢复后仍可走 SessionGit 回滚
- 保存时机  ：每次登记/裁决/checkpoint 后由 ReviewService 调用 save
- 孤儿快照  ：save 时清理（记录已回滚/被丢弃的快照文件）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from .ledger import FileChangeRecord

logger = logging.getLogger(__name__)

_LEDGER_FILE = "review-ledger.json"
_SNAPSHOT_DIR = "review-snapshots"

# FileChangeRecord 的纯数据字段（before_text 走快照文件，不进 JSON）
_RECORD_FIELDS = (
    "id", "tool_call_id", "tool_name", "path", "kind", "turn_id",
    "existed_before", "snapshot_bytes", "pre_commit", "fallback_git",
    "diff", "lines_added", "lines_removed", "status",
)


class LedgerStore:
    """单会话台账存储（session_dir 由 SessionManager 提供）"""

    def __init__(self, session_dir: Path) -> None:
        self._dir = Path(session_dir)

    @property
    def path(self) -> Path:
        return self._dir / _LEDGER_FILE

    # ============================================================
    # save / load
    # ============================================================

    def save(self, records: Iterable[FileChangeRecord], next_id: int) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        snap_dir = self._dir / _SNAPSHOT_DIR
        snap_dir.mkdir(exist_ok=True)

        entries: list[dict] = []
        live_snapshots: set[str] = set()
        for record in records:
            entry = {field: getattr(record, field) for field in _RECORD_FIELDS}
            if record.before_text is not None:
                snap_name = f"{record.id}.txt"
                (snap_dir / snap_name).write_bytes(record.before_text.encode("utf-8"))
                entry["has_snapshot"] = True
                live_snapshots.add(snap_name)
            else:
                entry["has_snapshot"] = False
            entries.append(entry)

        # 清理不再被任何记录引用的孤儿快照
        for stale in snap_dir.glob("*.txt"):
            if stale.name not in live_snapshots:
                stale.unlink(missing_ok=True)

        payload = {"version": 1, "next_id": next_id, "records": entries}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
        )

    def load(self) -> tuple[list[FileChangeRecord], int]:
        """恢复记录与 next_id；文件缺失返回空，损坏则告警后从空开始"""
        if not self.path.is_file():
            return [], 1
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            entries = payload["records"]
            next_id = int(payload["next_id"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("[review] ledger file unreadable, starting empty: %s", self.path)
            return [], 1

        records: list[FileChangeRecord] = []
        for entry in entries:
            try:
                records.append(self._restore_record(entry))
            except (KeyError, TypeError) as exc:
                logger.warning("[review] skip malformed ledger entry: %s (%s)", entry.get("id"), exc)
        return records, max(next_id, len(records) + 1)

    def _restore_record(self, entry: dict) -> FileChangeRecord:
        kwargs = {field: entry[field] for field in _RECORD_FIELDS}
        before_text: str | None = None
        if entry.get("has_snapshot"):
            snap = self._dir / _SNAPSHOT_DIR / f"{entry['id']}.txt"
            try:
                before_text = snap.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("[review] snapshot for #%s unreadable: %s", entry["id"], exc)
        return FileChangeRecord(before_text=before_text, **kwargs)
