"""ChangeLedger 变更台账测试：登记 / 回滚 undo 栈 / git 兜底 / 生命周期"""
from __future__ import annotations

from pathlib import Path

from dot.agent.tools import AgentToolResult
from dot.ai.types import ToolCall
from dot.coding.review.ledger import (
    STATUS_ACCEPTED,
    STATUS_ACCEPTED_AUTO,
    STATUS_PENDING,
    STATUS_REVERTED,
    ChangeLedger,
)
from dot.coding.session.git import SessionGit


# ============================================================
# 工具函数
# ============================================================

def _write_call(ledger: ChangeLedger, ws: Path, rel: str, content: str,
                call_id: str = "c1", is_error: bool = False) -> None:
    """模拟一次 write_file：before → 落盘 → after"""
    call = ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": rel, "content": content})
    ledger.before(call)
    if not is_error:
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    ledger.after(call, AgentToolResult(), is_error)


def _edit_call(ledger: ChangeLedger, ws: Path, rel: str, old: str, new: str,
               call_id: str, is_error: bool = False) -> None:
    call = ToolCall(id=call_id, name="edit_file",
                    arguments={"file_path": rel, "old_text": old, "new_text": new})
    ledger.before(call)
    if not is_error:
        path = ws / rel
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    ledger.after(call, AgentToolResult(), is_error)


# ============================================================
# 登记
# ============================================================

def test_write_create_is_recorded_as_pending_create(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "new.py", "a = 1\n")
    pending = ledger.pending_records()
    assert len(pending) == 1
    record = pending[0]
    assert record.kind == "create"
    assert record.path == "new.py"
    assert record.status == STATUS_PENDING
    assert record.turn_id == 0
    assert record.existed_before is False
    assert "+a = 1" in record.diff


def test_write_update_is_recorded_with_snapshot(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "app.py", "v2\n")
    record = ledger.pending_records()[0]
    assert record.kind == "update"
    assert record.before_text == "v1\n"
    assert record.fallback_git is False


def test_failed_execution_is_not_recorded(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "x.py", "a\n", is_error=True)
    assert ledger.pending_records() == []


def test_no_change_write_is_not_recorded(tmp_path: Path) -> None:
    (tmp_path / "same.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "same.py", "x = 1\n")
    assert ledger.pending_records() == []


def test_non_write_tool_is_ignored(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    call = ToolCall(id="b1", name="bash", arguments={"command": "echo hi"})
    ledger.before(call)
    ledger.after(call, AgentToolResult(), False)
    assert ledger.pending_records() == []


def test_blocked_call_snapshot_is_dropped_at_checkpoint(tmp_path: Path) -> None:
    """被拦截的调用：有快照无 after → checkpoint 时清理，不产生记录"""
    ledger = ChangeLedger(tmp_path)
    call = ToolCall(id="blocked", name="write_file",
                    arguments={"file_path": "x.py", "content": "a\n"})
    ledger.before(call)
    ledger.discard("blocked")
    fresh = ledger.checkpoint_turn(1)
    assert fresh == []
    assert ledger.pending_records() == []


def test_checkpoint_assigns_turn_id(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "x.py", "a\n", call_id="c1")
    fresh = ledger.checkpoint_turn(7)
    assert [r.turn_id for r in fresh] == [7]


# ============================================================
# 回滚：undo 栈
# ============================================================

def test_revert_update_restores_previous_content(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "app.py", "v2\n", call_id="c1")
    message = ledger.revert(1)
    assert "reverted" in message
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v1\n"
    assert ledger.records[0].status == STATUS_REVERTED


def test_revert_create_deletes_file(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "generated.py", "print('hi')\n")
    ledger.revert(1)
    assert not (tmp_path / "generated.py").exists()


def test_revert_earlier_record_cascades_later_ones(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "app.py", "v2\n", call_id="c1")
    _write_call(ledger, tmp_path, "app.py", "v3\n", call_id="c2")
    _edit_call(ledger, tmp_path, "app.py", "v3", "v3final", call_id="c3")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v3final\n"

    message = ledger.revert(1)  # 回滚最早一条 → 后两条级联回滚
    assert "cascaded" in message
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v1\n"
    statuses = {r.id: r.status for r in ledger.records}
    assert statuses == {1: STATUS_REVERTED, 2: STATUS_REVERTED, 3: STATUS_REVERTED}


def test_double_revert_is_reported(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "x.py", "a\n")
    ledger.revert(1)
    assert "already reverted" in ledger.revert(1)


def test_revert_all_rolls_back_everything(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a1\n", encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "a.py", "a2\n", call_id="c1")
    _write_call(ledger, tmp_path, "b.py", "b1\n", call_id="c2")
    ledger.revert_all()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a1\n"
    assert not (tmp_path / "b.py").exists()


def test_binary_write_recorded_without_diff_and_revert_deletes(tmp_path: Path) -> None:
    """写入含 NUL 的内容：登记但 diff 为空（二进制无意义），新建类回滚即删除"""
    ledger = ChangeLedger(tmp_path)
    call = ToolCall(id="b1", name="write_file",
                    arguments={"file_path": "blob.py", "content": "a\x00b"})
    ledger.before(call)
    (tmp_path / "blob.py").write_bytes(b"a\x00b")
    ledger.after(call, AgentToolResult(), False)

    record = ledger.pending_records()[0]
    assert record.kind == "create"
    assert record.diff == ""  # 二进制不做文本 diff
    ledger.revert(1)
    assert not (tmp_path / "blob.py").exists()


def test_binary_update_existing_uses_git_fallback(tmp_path: Path) -> None:
    """二进制旧文件被覆盖：无内存快照 → fallback_git，revert 走 SessionGit"""
    git = SessionGit(tmp_path / ".dot" / "sessions" / "s1", tmp_path)
    git.init()
    (tmp_path / "blob.bin").write_bytes(b"old\x00bytes")
    git.commit_turn(0)

    ledger = ChangeLedger(tmp_path, git_provider=lambda: git)
    call = ToolCall(id="b1", name="write_file",
                    arguments={"file_path": "blob.bin", "content": "new"})
    ledger.before(call)
    (tmp_path / "blob.bin").write_bytes(b"new\x00data")
    ledger.after(call, AgentToolResult(), False)

    record = ledger.pending_records()[0]
    assert record.fallback_git is True
    assert record.pre_commit == git.head()
    ledger.revert(1)
    assert (tmp_path / "blob.bin").read_bytes() == b"old\x00bytes"


def test_revert_unknown_id(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    assert "no such change" in ledger.revert(99)


# ============================================================
# git 兜底与生命周期
# ============================================================

def test_oversized_snapshot_falls_back_to_git(tmp_path: Path) -> None:
    big = "x" * (600 * 1024)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8", newline="\n")
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "big.txt", big + "more")
    record = ledger.pending_records()[0]
    assert record.fallback_git is True
    assert record.before_text is None


def test_git_fallback_revert_via_session_git(tmp_path: Path) -> None:
    """内存快照超限时，revert 走 SessionGit 恢复到登记时的 HEAD"""
    git = SessionGit(tmp_path / ".dot" / "sessions" / "s1", tmp_path)
    git.init()
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    git.commit_turn(0)  # HEAD 包含 baseline 版本

    big_new = "y" * (600 * 1024)
    (tmp_path / "tracked.txt").write_text(big_new, encoding="utf-8")
    ledger = ChangeLedger(tmp_path, git_provider=lambda: git)
    _write_call(ledger, tmp_path, "tracked.txt", big_new + "!", call_id="c1")
    record = ledger.pending_records()[0]
    assert record.fallback_git is True
    assert record.pre_commit == git.head()

    ledger.revert(1)
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"


def test_session_git_restore_paths_new_file_is_deleted(tmp_path: Path) -> None:
    git = SessionGit(tmp_path / ".dot" / "sessions" / "s1", tmp_path)
    git.init()
    new_file = tmp_path / "created.txt"
    new_file.write_text("temp\n", encoding="utf-8")
    git.restore_paths(["created.txt"])
    assert not new_file.exists()


def test_accept_and_auto_resolve_statuses(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "a.py", "a\n", call_id="c1")
    _write_call(ledger, tmp_path, "b.py", "b\n", call_id="c2")
    assert ledger.accept(1) .startswith("accepted")
    assert ledger.pending_records()[0].id == 2
    assert ledger.auto_resolve_pending() == 1
    statuses = {r.id: r.status for r in ledger.records}
    assert statuses == {1: STATUS_ACCEPTED, 2: STATUS_ACCEPTED_AUTO}


def test_on_rewind_marks_later_records_reverted(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "a.py", "a\n", call_id="c1")
    ledger.checkpoint_turn(1)
    _write_call(ledger, tmp_path, "b.py", "b\n", call_id="c2")
    ledger.checkpoint_turn(2)
    ledger.on_rewind(1)
    statuses = {r.id: r.status for r in ledger.records}
    assert statuses == {1: STATUS_PENDING, 2: STATUS_REVERTED}


def test_reset_session_clears_everything(tmp_path: Path) -> None:
    ledger = ChangeLedger(tmp_path)
    _write_call(ledger, tmp_path, "a.py", "a\n")
    ledger.reset_session()
    assert ledger.records == ()
    assert ledger.pending_records() == []
