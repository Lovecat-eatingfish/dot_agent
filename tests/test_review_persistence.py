"""台账持久化测试：save/load 往返、快照影子文件、resume 恢复、孤儿快照清理"""
from __future__ import annotations

import asyncio
from pathlib import Path

from dot.ai.types import ToolCall
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode
from dot.coding.review.persistence import LedgerStore
from test_review_e2e import ScriptedProvider, _write_call  # 复用脚本化 Provider


# ============================================================
# LedgerStore 单元
# ============================================================

def _make_record(tmp_path: Path, record_id: int = 1, **overrides):
    from dot.coding.review.ledger import FileChangeRecord, STATUS_PENDING

    defaults = dict(
        id=record_id, tool_call_id=f"c{record_id}", tool_name="write_file",
        path="a.py", kind="create", turn_id=1, existed_before=False,
        snapshot_bytes=0, pre_commit="", fallback_git=False,
        diff="+a\n", lines_added=1, lines_removed=0, status=STATUS_PENDING,
    )
    defaults.update(overrides)
    return FileChangeRecord(**defaults)


def test_ledger_store_round_trip_with_snapshot(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path / "session-dir")
    record = _make_record(tmp_path)
    record.before_text = "v1\n"
    record.snapshot_bytes = 3

    store.save([record], next_id=2)
    assert (tmp_path / "session-dir" / "review-ledger.json").is_file()
    assert (tmp_path / "session-dir" / "review-snapshots" / "1.txt").read_bytes() == b"v1\n"

    records, next_id = store.load()
    assert next_id == 2
    assert len(records) == 1
    assert records[0].before_text == "v1\n"
    assert records[0].path == "a.py"
    assert records[0].status == record.status


def test_ledger_store_round_trip_fallback_git(tmp_path: Path) -> None:
    """fallback_git 记录没有内存快照：恢复后靠 pre_commit + SessionGit 兜底"""
    store = LedgerStore(tmp_path / "session-dir")
    record = _make_record(tmp_path, fallback_git=True, pre_commit="abc123", before_text=None)
    store.save([record], next_id=5)

    records, next_id = store.load()
    assert next_id == 5
    assert records[0].fallback_git is True
    assert records[0].pre_commit == "abc123"
    assert records[0].before_text is None


def test_ledger_store_orphan_snapshots_pruned(tmp_path: Path) -> None:
    snap_dir = tmp_path / "session-dir" / "review-snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "999.txt").write_bytes(b"stale")

    store = LedgerStore(tmp_path / "session-dir")
    record = _make_record(tmp_path)
    record.before_text = "x"
    store.save([record], next_id=2)

    assert not (snap_dir / "999.txt").exists()
    assert (snap_dir / "1.txt").exists()


def test_ledger_store_corrupted_file_starts_empty(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-dir"
    session_dir.mkdir()
    (session_dir / "review-ledger.json").write_text("{broken", encoding="utf-8")
    records, next_id = LedgerStore(session_dir).load()
    assert records == [] and next_id == 1


def test_ledger_store_missing_file_starts_empty(tmp_path: Path) -> None:
    records, next_id = LedgerStore(tmp_path / "nope").load()
    assert records == [] and next_id == 1


# ============================================================
# ReviewService 持久化接线
# ============================================================

def test_service_reload_restores_pending_and_revert(tmp_path: Path) -> None:
    """登记 → 新 service 实例 reload → pending 恢复 → revert 靠快照恢复文件"""
    from dot.coding.review.service import ReviewService

    (tmp_path / "a.py").write_text("v1\n", encoding="utf-8", newline="\n")
    store = LedgerStore(tmp_path / "session-dir")

    service = ReviewService(tmp_path, store_provider=lambda: store, auto_resolve=False)
    call = ToolCall(id="c1", name="write_file",
                    arguments={"file_path": "a.py", "content": "v2\n"})
    service.before_tool_call(call)
    (tmp_path / "a.py").write_text("v2\n", encoding="utf-8", newline="\n")
    service.after_tool_call(call, None, False)
    assert service.pending_count == 1

    # 新实例（模拟进程重启 / 会话切换）加载
    service2 = ReviewService(tmp_path, store_provider=lambda: store, auto_resolve=False)
    service2.reload_session()
    assert service2.pending_count == 1
    record = service2.ledger.pending_records()[0]
    assert record.before_text == "v1\n"

    assert "reverted #1" in service2.revert("1")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "v1\n"
    # 回滚后的状态也持久化
    records, _ = store.load()
    assert records[0].status == "reverted"


# ============================================================
# host 级 resume 恢复
# ============================================================

def _run_prompt(harness, text: str) -> None:
    async def _run() -> None:
        async for _ in harness.prompt(text):
            pass

    asyncio.run(_run())


def test_resume_session_restores_ledger(tmp_path: Path) -> None:
    """host1 写文件落盘 → host2 resume 同一会话 → 台账恢复、仍可回滚"""
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    host1 = CodingHost(workspace=tmp_path, mode=AgentMode.AUTO)
    harness1 = host1.create_harness(system="t")
    harness1._config.provider = ScriptedProvider(
        [[_write_call("c1", "app.py", "v2\n")], "done"],
    )
    _run_prompt(harness1, "改成 v2")
    host1.end_turn()  # auto_resolve=True → accepted-auto 且已持久化
    sid = host1.session_id

    host2 = CodingHost(workspace=tmp_path, mode=AgentMode.AUTO)
    assert host2.resume_session(sid) is True
    records = host2.review.ledger.records
    assert len(records) == 1
    assert records[0].status == "accepted-auto"
    assert records[0].before_text == "v1\n"
    assert host2.review.pending_count == 0  # accepted-auto 不占 pending

    host2.review.revert("1")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v1\n"


def test_resume_session_restores_pending_review(tmp_path: Path) -> None:
    """交互模式（auto_resolve=False）下 pending 列表跨进程恢复"""
    host1 = CodingHost(workspace=tmp_path, mode=AgentMode.AUTO)
    host1.review.auto_resolve = False
    harness1 = host1.create_harness(system="t")
    harness1._config.provider = ScriptedProvider(
        [[_write_call("c1", "new.py", "hello\n")], "done"],
    )
    _run_prompt(harness1, "写文件")
    host1.end_turn()
    sid = host1.session_id
    assert host1.review.pending_count == 1

    host2 = CodingHost(workspace=tmp_path, mode=AgentMode.AUTO)
    assert host2.resume_session(sid) is True
    assert host2.review.pending_count == 1
    pending = host2.review.ledger.pending_records()[0]
    assert pending.path == "new.py"
    assert pending.before_text is None and pending.existed_before is False

    host2.review.revert("1")  # 新建文件回滚 = 删除
    assert not (tmp_path / "new.py").exists()
