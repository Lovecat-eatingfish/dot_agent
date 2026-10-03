"""ReviewService 门面测试：turn 收尾裁决 / 命令语义 / 汇总文本"""
from __future__ import annotations

from pathlib import Path

from dot.agent.tools import AgentToolResult
from dot.ai.types import ToolCall
from dot.coding.review.service import ReviewService


def _record_write(service: ReviewService, ws: Path, rel: str, content: str, call_id: str) -> None:
    call = ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": rel, "content": content})
    service.before_tool_call(call)
    (ws / rel).write_text(content, encoding="utf-8", newline="\n")
    service.after_tool_call(call, AgentToolResult(), False)


def test_headless_checkpoint_auto_accepts(tmp_path: Path) -> None:
    """无头环境（auto_resolve=True）：turn 收尾自动接受 pending"""
    service = ReviewService(tmp_path, auto_resolve=True)
    _record_write(service, tmp_path, "a.py", "a\n", "c1")
    assert service.pending_count == 1
    fresh = service.checkpoint_turn(1)
    assert len(fresh) == 1
    assert service.pending_count == 0
    assert fresh[0].status == "accepted-auto"
    assert fresh[0].turn_id == 1


def test_interactive_checkpoint_keeps_pending(tmp_path: Path) -> None:
    service = ReviewService(tmp_path, auto_resolve=False)
    _record_write(service, tmp_path, "a.py", "a\n", "c1")
    service.checkpoint_turn(1)
    assert service.pending_count == 1


def test_list_and_show_text(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8", newline="\n")
    service = ReviewService(tmp_path)
    _record_write(service, tmp_path, "a.py", "new\n", "c1")
    listing = service.list_text()
    assert "#1" in listing and "a.py" in listing
    shown = service.show_text(1)
    assert "-old" in shown and "+new" in shown
    assert "no such change" in service.show_text(42)


def test_accept_revert_command_semantics(tmp_path: Path) -> None:
    service = ReviewService(tmp_path)
    _record_write(service, tmp_path, "a.py", "a\n", "c1")
    _record_write(service, tmp_path, "b.py", "b\n", "c2")
    assert "accepted #1" in service.accept("1")
    assert "accepted" in service.accept("all")
    assert "no pending changes" in service.accept("all")
    assert "reverted" in service.revert("#1")
    assert "already reverted" in service.revert(1)
    # a.py 是新建文件：revert = 删除
    assert not (tmp_path / "a.py").exists()
    assert "usage" in service.revert("not-an-id")


def test_revert_all_command_semantics(tmp_path: Path) -> None:
    service = ReviewService(tmp_path)
    _record_write(service, tmp_path, "a.py", "a\n", "c1")
    message = service.revert("all")
    assert "reverted 1 change(s)" in message
    assert not (tmp_path / "a.py").exists()


def test_pending_summary_text(tmp_path: Path) -> None:
    service = ReviewService(tmp_path)
    assert service.pending_summary_text() == ""
    _record_write(service, tmp_path, "a.py", "a\n", "c1")
    summary = service.pending_summary_text()
    assert "1 file change(s) pending" in summary
    assert "a.py" in summary and "/review" in summary
