"""权限评审模式矩阵与会话级 accept-edits 测试"""
from __future__ import annotations

import asyncio
from pathlib import Path

from dot.agent.permission import Decision
from dot.coding.permission import PermissionManager


def _manager(tmp_path: Path, deny_patterns: list[str] | None = None) -> PermissionManager:
    manager = PermissionManager()
    if deny_patterns is not None:
        import json

        (tmp_path / ".agent-security.json").write_text(
            json.dumps({"denyFilePatterns": deny_patterns}), encoding="utf-8",
        )
    manager.load_project(tmp_path)
    return manager


# ============================================================
# 模式矩阵
# ============================================================

def test_plan_mode_file_write_asks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    decision = manager.check("write_file", {"file_path": "a.py"}, agent_mode="plan")
    assert decision.decision is Decision.ASK


def test_edit_mode_file_write_asks(tmp_path: Path) -> None:
    """EDIT 模式写操作从 ALLOW 改为 ASK（本设计的行为变更）"""
    manager = _manager(tmp_path)
    decision = manager.check("edit_file", {"file_path": "a.py"}, agent_mode="edit")
    assert decision.decision is Decision.ASK


def test_edit_mode_bash_asks_and_read_allows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.check("bash", {"command": "ls"}, agent_mode="edit").decision is Decision.ASK
    assert manager.check("read_file", {"file_path": "a.py"}, agent_mode="edit").decision is Decision.ALLOW


def test_auto_mode_file_write_allows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    decision = manager.check("write_file", {"file_path": "a.py"}, agent_mode="auto")
    assert decision.decision is Decision.ALLOW


# ============================================================
# 会话级 accept-edits
# ============================================================

def test_session_accept_edits_bypasses_edit_write_ask(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.check("write_file", {"file_path": "a.py"}, agent_mode="edit").decision is Decision.ASK
    manager.enable_session_accept_edits()
    assert manager.check("write_file", {"file_path": "a.py"}, agent_mode="edit").decision is Decision.ALLOW
    assert manager.check("edit_file", {"file_path": "a.py"}, agent_mode="auto").decision is Decision.ALLOW


def test_session_accept_edits_does_not_bypass_plan(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.enable_session_accept_edits()
    assert manager.check("write_file", {"file_path": "a.py"}, agent_mode="plan").decision is Decision.ASK


def test_session_accept_edits_does_not_bypass_bash_ask(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.enable_session_accept_edits()
    assert manager.check("bash", {"command": "ls"}, agent_mode="edit").decision is Decision.ASK


def test_reset_session_state_clears_accept_edits(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.enable_session_accept_edits()
    manager.reset_session_state()
    assert manager.session_accepts_edits() is False
    assert manager.check("write_file", {"file_path": "a.py"}, agent_mode="edit").decision is Decision.ASK


# ============================================================
# 黑名单优先级（评审机制不可放行）
# ============================================================

def test_session_accept_cannot_bypass_path_traversal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.enable_session_accept_edits()
    decision = manager.check(
        "write_file", {"file_path": "../outside.txt"}, agent_mode="edit",
    )
    assert decision.decision is Decision.DENY


def test_session_accept_cannot_bypass_project_deny_patterns(tmp_path: Path) -> None:
    manager = _manager(tmp_path, deny_patterns=["**/.env"])
    manager.enable_session_accept_edits()
    decision = manager.check("write_file", {"file_path": ".env"}, agent_mode="edit")
    assert decision.decision is Decision.DENY


def test_system_blacklist_beats_approved_bypass(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    decision = manager.check(
        "write_file", {"file_path": "../outside.txt"}, agent_mode="edit", approved=True,
    )
    assert decision.decision is Decision.DENY


# ============================================================
# ask_user：预览注入 + 拒绝文案
# ============================================================

def test_ask_user_injects_diff_preview(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    manager = _manager(tmp_path)
    seen: dict = {}

    def handler(info: dict) -> bool:
        seen.update(info)
        return True

    manager.set_approval_handler(handler)
    approved = asyncio.run(manager.ask_user(
        "write_file", {"file_path": "app.py", "content": "v2\n"},
        manager.check("write_file", {"file_path": "app.py"}, agent_mode="edit"),
        agent_mode="edit",
    ))
    assert approved is True
    preview = seen["preview"]
    assert preview["kind"] == "update"
    assert preview["path"] == "app.py"
    assert "-v1" in preview["diff"] and "+v2" in preview["diff"]


def test_ask_user_rejection_sets_deny_override(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    decision = manager.check("write_file", {"file_path": "a.py"}, agent_mode="edit")
    manager.set_approval_handler(lambda info: False)
    approved = asyncio.run(manager.ask_user(
        "write_file", {"file_path": "a.py", "content": "x"}, decision, agent_mode="edit",
    ))
    assert approved is False
    assert decision.deny_message() == "user rejected the change"


def test_ask_user_without_handler_auto_denies(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    decision = manager.check("write_file", {"file_path": "a.py"}, agent_mode="edit")
    approved = asyncio.run(manager.ask_user(
        "write_file", {"file_path": "a.py", "content": "x"}, decision, agent_mode="edit",
    ))
    assert approved is False
    # 无头自动拒绝不是"用户拒绝"，保留默认拦截文案
    assert decision.deny_message() != "user rejected the change"
