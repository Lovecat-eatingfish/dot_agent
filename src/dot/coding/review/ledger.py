"""
dot.coding.review.ledger — ChangeLedger 会话级文件变更台账

事后评审的数据层（对标 Codex auto 模式的"生成后仍可接受/拒绝"）：

- before/after 两个 hook 入口由 host 组合进工具调用链（不侵入 agent 层）：
    before : 工具执行前捕获文件内容快照（内存，有上限）
    after  : 执行后登记 FileChangeRecord（内容未变/执行失败不登记）
- revert：单条回滚（undo 栈语义：同文件更晚的未回滚记录级联回滚）；
  内存快照不可用时走 SessionGit（restore 到该记录创建时的 HEAD）
- checkpoint_turn：turn 收尾回填 turn_id、丢弃被拦截调用的残留快照

状态机：pending → accepted / accepted-auto / reverted（终态）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dot.ai.types import ToolCall

from .fsutil import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_SESSION_BYTES,
    decode_text_lossy,
    diff_stat,
    display_path,
    is_probably_binary,
    resolve,
    unified_diff,
)

logger = logging.getLogger(__name__)

# 与 dot.coding.permission.FILE_WRITE_TOOLS 保持同一组工具；
# 此处独立定义避免 ledger → permission 的模块级依赖
FILE_WRITE_TOOLS = {"write_file", "edit_file"}

# FileChangeRecord.status 取值
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_ACCEPTED_AUTO = "accepted-auto"
STATUS_REVERTED = "reverted"

# 台账内保留的最大 diff 长度（展示时再二次截断）
_MAX_RECORD_DIFF_CHARS = 20000


@dataclass
class FileChangeRecord:
    """一次文件写工具造成的已落盘变更"""
    id: int
    tool_call_id: str = ""
    tool_name: str = ""
    path: str = ""                       # workspace 相对 posix 路径
    kind: str = "update"                 # create | update
    turn_id: int = 0                     # checkpoint 时回填
    existed_before: bool = False
    before_text: str | None = None       # None = 无内存快照（二进制/超限/读取失败）
    snapshot_bytes: int = 0
    pre_commit: str = ""                 # 登记时的 git HEAD（git 兜底回滚目标）
    fallback_git: bool = False           # True = 回滚只能走 SessionGit
    diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0
    status: str = STATUS_PENDING

    @property
    def revertible(self) -> bool:
        return self.status in (STATUS_PENDING, STATUS_ACCEPTED, STATUS_ACCEPTED_AUTO)


@dataclass(slots=True)
class _PreState:
    """工具执行前的文件状态（挂在 tool_call_id 上）"""
    existed: bool = False
    text: str | None = None              # None = 未捕获内存快照
    readable: bool = False


class ChangeLedger:
    """会话级变更台账（同 host 同会话一个实例）"""

    def __init__(
        self,
        workspace: Path,
        git_provider: Callable[[], object | None] | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._git_provider = git_provider or (lambda: None)
        self._records: list[FileChangeRecord] = []
        self._pending: dict[str, _PreState] = {}   # tool_call_id -> 执行前状态
        self._next_id = 1
        self._stored_bytes = 0
        self._turn_head = ""                       # 本 turn 的 HEAD 缓存（turn 内无 commit）
        self._turn_head_captured = False

    # ============================================================
    # hook 入口（host 装配时组合进 before/after 工具链）
    # ============================================================

    def before(self, call: ToolCall) -> None:
        """工具执行前捕获文件快照；捕获失败不阻塞工具执行"""
        if call.name not in FILE_WRITE_TOOLS:
            return
        pre = _PreState()
        try:
            path = resolve(self._workspace, str(call.arguments.get("file_path", "")))
            pre.existed = path.exists()
            if pre.existed:
                raw = path.read_bytes()
                if (
                    not is_probably_binary(raw)
                    and len(raw) <= MAX_SNAPSHOT_FILE_BYTES
                    and self._stored_bytes + len(raw) <= MAX_SNAPSHOT_SESSION_BYTES
                ):
                    pre.text = decode_text_lossy(raw)
                    pre.readable = True
        except OSError as exc:
            logger.warning("[review] snapshot before %s failed: %s", call.id, exc)
        if not self._turn_head_captured:
            git = self._git_provider()
            self._turn_head = git.head() if git is not None and git.available else ""
            self._turn_head_captured = True
        self._pending[call.id] = pre

    def discard(self, call_id: str) -> None:
        """调用被拦截/取消时丢弃快照（after 也会兜底清理）"""
        self._pending.pop(call_id, None)

    def after(self, call: ToolCall, result: AgentToolResult, is_error: bool) -> FileChangeRecord | None:
        """工具执行后登记变更；执行失败或内容未变不登记。返回新记录（或 None）"""
        pre = self._pending.pop(call.id, None)
        if pre is None or call.name not in FILE_WRITE_TOOLS:
            return
        if is_error:
            return
        try:
            path = resolve(self._workspace, str(call.arguments.get("file_path", "")))
            if not path.exists():
                return
            raw = path.read_bytes()
            post = decode_text_lossy(raw)
        except OSError as exc:
            logger.warning("[review] read after %s failed: %s", call.id, exc)
            return
        if pre.existed and pre.text is not None and post == pre.text:
            return  # 内容未变（edit 未命中、重复写入）——不算变更

        display = display_path(self._workspace, path)
        diff = "" if is_probably_binary(raw) else unified_diff(pre.text or "", post, display)
        added, removed = diff_stat(diff)
        fallback = pre.existed and not pre.readable
        record = FileChangeRecord(
            id=self._next_id,
            tool_call_id=call.id,
            tool_name=call.name,
            path=display,
            kind="update" if pre.existed else "create",
            existed_before=pre.existed,
            before_text=pre.text,
            snapshot_bytes=len(pre.text.encode("utf-8")) if pre.readable else 0,
            pre_commit=self._turn_head,
            fallback_git=fallback,
            diff=diff[:_MAX_RECORD_DIFF_CHARS],
            lines_added=added,
            lines_removed=removed,
        )
        self._next_id += 1
        self._stored_bytes += record.snapshot_bytes
        self._records.append(record)
        logger.info("[review] #%d %s %s (%+d/-%d)", record.id, record.kind, record.path,
                    record.lines_added, record.lines_removed)
        return record

    # ============================================================
    # 裁决：accept / revert
    # ============================================================

    def accept(self, record_id: int) -> str:
        record = self._find(record_id)
        if record is None:
            return f"no such change: #{record_id}"
        if record.status == STATUS_REVERTED:
            return f"#{record_id} already reverted, cannot accept"
        if record.status != STATUS_PENDING:
            return f"#{record_id} already {record.status}"
        record.status = STATUS_ACCEPTED
        return f"accepted #{record_id} {record.path}"

    def accept_all(self) -> int:
        """接受全部 pending，返回数量（无头 turn 收尾用 accepted-auto）"""
        return self._resolve_all(STATUS_ACCEPTED)

    def auto_resolve_pending(self) -> int:
        """无头环境 turn 收尾：全部标记 accepted-auto"""
        return self._resolve_all(STATUS_ACCEPTED_AUTO)

    def _resolve_all(self, status: str) -> int:
        count = 0
        for record in self._records:
            if record.status == STATUS_PENDING:
                record.status = status
                count += 1
        return count

    def revert(self, record_id: int) -> str:
        """回滚一条变更；同路径更晚的未回滚记录级联回滚（undo 栈语义）"""
        return self.revert_collecting(record_id)[0]

    def revert_collecting(self, record_id: int) -> tuple[str, list[FileChangeRecord]]:
        """revert 的结构化版本：返回 (消息, 实际回滚的记录列表，含级联)"""
        record = self._find(record_id)
        if record is None:
            return f"no such change: #{record_id}", []
        if not record.revertible:
            return f"#{record_id} already reverted", []
        later = [
            r for r in self._records
            if r.path == record.path and r.id > record.id and r.revertible
        ]
        reverted: list[FileChangeRecord] = []
        for r in reversed(later):
            if self._revert_one(r):
                reverted.append(r)
        if self._revert_one(record):
            reverted.append(record)
        cascade = f" (+{len(later)} cascaded)" if later else ""
        return f"reverted #{record.id} {record.path}{cascade}", reverted

    def revert_all(self) -> str:
        """回滚全部未回滚记录（从晚到早）"""
        return self.revert_all_collecting()[0]

    def revert_all_collecting(self) -> tuple[str, list[FileChangeRecord]]:
        """revert_all 的结构化版本：返回 (消息, 实际回滚的记录列表)"""
        targets = [r.id for r in self._records if r.revertible]
        if not targets:
            return "no pending changes to revert", []
        reverted: list[FileChangeRecord] = []
        for rid in reversed(targets):
            record = self._find(rid)
            if record is not None and record.revertible and self._revert_one(record):
                reverted.append(record)
        return f"reverted {len(reverted)} change(s)", reverted

    def _revert_one(self, record: FileChangeRecord) -> bool:
        """执行单条回滚；返回是否成功（失败保持原状态可重试）"""
        path = self._workspace / record.path
        try:
            if record.existed_before and record.before_text is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(record.before_text, encoding="utf-8", newline="\n")
            elif not record.existed_before:
                path.unlink(missing_ok=True)
            else:
                # existed_before 但无内存快照 → SessionGit 兜底（恢复到登记时的 HEAD）
                self._revert_via_git(record)
        except Exception as exc:
            logger.warning("[review] revert #%s failed: %s", record.id, exc)
            return False
        record.status = STATUS_REVERTED
        if record.before_text is not None:
            self._stored_bytes = max(0, self._stored_bytes - record.snapshot_bytes)
            record.before_text = None
            record.snapshot_bytes = 0
        return True

    def _revert_via_git(self, record: FileChangeRecord) -> None:
        git = self._git_provider()
        if git is None or not git.available:
            raise RuntimeError("memory snapshot unavailable and git snapshot disabled")
        git.restore_paths([record.path], commit=record.pre_commit or None)

    # ============================================================
    # 生命周期：turn 收尾 / 会话切换 / rewind 联动
    # ============================================================

    def checkpoint_turn(self, turn_id: int) -> list[FileChangeRecord]:
        """turn 收尾：给本轮创建的记录回填 turn_id，丢弃被拦截调用的残留快照"""
        fresh = [r for r in self._records if r.turn_id == 0]
        for record in fresh:
            record.turn_id = turn_id
        self._pending.clear()
        self._turn_head_captured = False
        return fresh

    def on_rewind(self, turn_id: int) -> int:
        """/rewind 到 turn_id 后：其后的变更文件已被 git reset 回滚，台账同步标记"""
        count = 0
        for record in self._records:
            if (record.turn_id == 0 or record.turn_id > turn_id) and record.revertible:
                record.status = STATUS_REVERTED
                self._stored_bytes = max(0, self._stored_bytes - record.snapshot_bytes)
                record.before_text = None
                record.snapshot_bytes = 0
                count += 1
        return count

    def reset_session(self) -> None:
        """切换会话：清空台账（记录归属旧会话）"""
        self._records.clear()
        self._pending.clear()
        self._next_id = 1
        self._stored_bytes = 0
        self._turn_head = ""
        self._turn_head_captured = False

    # ============================================================
    # 持久化状态导入/导出（review.persistence 使用）
    # ============================================================

    def export_state(self) -> tuple[list[FileChangeRecord], int]:
        """导出记录与下一个 id（供 LedgerStore.save）"""
        return list(self._records), self._next_id

    def import_state(self, records: list[FileChangeRecord], next_id: int) -> None:
        """导入持久化记录（供 LedgerStore.load）；快照字节量按记录重算"""
        self._records = list(records)
        self._next_id = max(1, int(next_id))
        self._stored_bytes = sum(r.snapshot_bytes for r in self._records)
        self._pending.clear()
        self._turn_head = ""
        self._turn_head_captured = False

    # ============================================================
    # 查询
    # ============================================================

    @property
    def records(self) -> tuple[FileChangeRecord, ...]:
        return tuple(self._records)

    def _find(self, record_id: int) -> FileChangeRecord | None:
        return next((r for r in self._records if r.id == record_id), None)

    def pending_records(self) -> list[FileChangeRecord]:
        return [r for r in self._records if r.status == STATUS_PENDING]
