"""文件变更评审端到端测试：脚本化 Provider 驱动真实 agent loop / 权限 / 台账 / 命令

不发真实 LLM 请求：ScriptedProvider 按剧本吐出带 ToolCall 的 assistant 消息，
其余全链路（权限三级拦截 → ASK 审批 → 工具执行 → 台账登记 → end_turn 落盘 →
/review 裁决）均为生产代码路径。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dot.agent.events import ToolExecutionEndEvent
from dot.agent.tools import AgentToolResult
from dot.ai import events as provider_events
from dot.ai.types import AssistantMessage, TextContent, ToolCall
from dot.coding.commands import get_command_registry
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode
from dot.coding.permission import reset_permission_manager


class ScriptedProvider:
    """按剧本回放 assistant 消息：list[ToolCall] = 工具轮，str = 纯文本收尾轮"""

    model = "fake"

    def __init__(self, script: list) -> None:
        self._script = list(script)

    async def stream_response(self, *, messages, **kwargs):
        del kwargs, messages
        turn = self._script.pop(0) if self._script else []
        if isinstance(turn, str):
            message = AssistantMessage(
                content=[TextContent(text=turn)], model=self.model, provider="fake",
                stop_reason="stop",
            )
        else:
            message = AssistantMessage(
                content=list(turn), model=self.model, provider="fake",
                stop_reason="toolUse",
            )
        yield provider_events.AssistantDoneEvent(reason=message.stop_reason, message=message)


def _write_call(call_id: str, rel: str, content: str) -> ToolCall:
    return ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": rel, "content": content})


def _make_host(tmp_path: Path, mode: AgentMode) -> CodingHost:
    reset_permission_manager()
    host = CodingHost(workspace=tmp_path, mode=mode)
    harness = host.create_harness(system="test")
    return host  # harness 挂在 host._harness；provider 由测试注入


def _script_provider(host: CodingHost, script: list) -> None:
    host._harness._config.provider = ScriptedProvider(script)


async def _run_turn(host: CodingHost, prompt: str) -> list:
    events = []
    async for event in host._harness.prompt(prompt):
        events.append(event)
    return events


# ============================================================
# EDIT 模式：拒绝
# ============================================================

def test_edit_mode_reject_blocks_write_and_reports_reason(tmp_path: Path) -> None:
    host = _make_host(tmp_path, AgentMode.EDIT)
    seen: dict = {}
    calls: list[bool] = []

    def handler(info: dict) -> bool:
        seen.update(info)
        calls.append(False)
        return False  # 用户拒绝

    host.permission.set_approval_handler(handler)
    _script_provider(host, [[_write_call("c1", "hello.py", "print('hi')\n")], "done"])

    events = asyncio.run(_run_turn(host, "写个脚本"))
    host.end_turn()

    # 拦截信息里带执行前 diff 预览（新建文件）
    assert seen["preview"]["kind"] == "create"
    assert seen["preview"]["path"] == "hello.py"
    assert "+print('hi')" in seen["preview"]["diff"]
    assert calls == [False]

    # 文件未落盘
    assert not (tmp_path / "hello.py").exists()
    # agent 收到明确的"用户拒绝"原因
    tool_ends = [e for e in events if isinstance(e, ToolExecutionEndEvent)]
    assert len(tool_ends) == 1 and tool_ends[0].is_error
    assert "user rejected the change" in tool_ends[0].result.text
    # 被拒调用不进台账
    assert host.review.ledger.records == ()


# ============================================================
# EDIT 模式：接受 → 台账 → /review 裁决
# ============================================================

def test_edit_mode_approve_then_review_commands(tmp_path: Path) -> None:
    host = _make_host(tmp_path, AgentMode.EDIT)
    host.permission.set_approval_handler(lambda info: True)  # 用户接受
    _script_provider(host, [[_write_call("c1", "hello.py", "print('hi')\n")], "done"])

    events = asyncio.run(_run_turn(host, "写个脚本"))
    assert not [e for e in events if isinstance(e, ToolExecutionEndEvent) and e.is_error]
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"

    # 交互模式（auto_resolve=False）：turn 结束后仍 pending，等待 /review
    host.review.auto_resolve = False
    host.end_turn()
    pending = host.review.ledger.pending_records()
    assert len(pending) == 1
    assert pending[0].kind == "create"
    assert pending[0].turn_id == 1

    # 真实斜杠命令链路
    registry = get_command_registry()
    registry.set_host(host)
    listing = registry.execute("/review")
    assert "#1" in listing.text and "hello.py" in listing.text
    summary = host.review.pending_summary_text()
    assert "1 file change(s) pending" in summary

    shown = registry.execute("/review show 1")
    assert "+print('hi')" in shown.text

    accepted = registry.execute("/review accept 1")
    assert "accepted #1" in accepted.text

    reverted = registry.execute("/review revert 1")
    assert "reverted #1" in reverted.text
    # 新建文件回滚 = 删除
    assert not (tmp_path / "hello.py").exists()
    assert "no pending file changes" in registry.execute("/review").text


# ============================================================
# EDIT 模式：会话级 accept-edits（a 键语义）
# ============================================================

def test_edit_mode_session_accept_edits_skips_subsequent_asks(tmp_path: Path) -> None:
    host = _make_host(tmp_path, AgentMode.EDIT)
    ask_count = {"n": 0}

    def handler(info: dict) -> bool:
        ask_count["n"] += 1
        host.permission.enable_session_accept_edits()  # 弹窗 a 键的行为
        return True

    host.permission.set_approval_handler(handler)
    _script_provider(host, [
        [_write_call("c1", "a.py", "a\n")],
        [_write_call("c2", "b.py", "b\n")],
        "done",
    ])

    async def _turns() -> None:
        async for _ in host._harness.prompt("first"):
            pass
        host.end_turn()
        async for _ in host._harness.prompt("second"):
            pass

    asyncio.run(_turns())
    assert ask_count["n"] == 1  # 第二次写不再弹审批
    assert (tmp_path / "a.py").exists() and (tmp_path / "b.py").exists()


# ============================================================
# AUTO 模式：不弹审批 + turn 结束自动接受
# ============================================================

def test_auto_mode_writes_silently_and_auto_accepts(tmp_path: Path) -> None:
    host = _make_host(tmp_path, AgentMode.AUTO)
    host.permission.set_approval_handler(lambda info: pytest.fail("AUTO 模式不应触发审批"))
    _script_provider(host, [[_write_call("c1", "auto.py", "x = 1\n")], "done"])

    asyncio.run(_run_turn(host, "随便写点"))
    assert (tmp_path / "auto.py").exists()  # 直接落盘，无审批

    # 无头默认 auto_resolve=True：turn 结束自动接受留痕
    host.end_turn()
    records = host.review.ledger.records
    assert len(records) == 1
    assert records[0].status == "accepted-auto"
    assert host.review.pending_count == 0


# ============================================================
# AUTO 模式：/review revert 回滚已自动接受的变更
# ============================================================

def test_auto_mode_revert_restores_file_content(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="\n")
    host = _make_host(tmp_path, AgentMode.AUTO)
    _script_provider(host, [[_write_call("c1", "app.py", "v2\n")], "done"])

    asyncio.run(_run_turn(host, "改成 v2"))
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v2\n"
    host.end_turn()

    host.review.revert("1")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v1\n"


# ============================================================
# TUI 评审弹窗（textual pilot）
# ============================================================

def test_tui_change_review_modal_keys(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    asyncio.run(_run_tui_modal(tmp_path))


async def _run_tui_modal(tmp_path: Path) -> None:
    from textual.screen import ModalScreen

    from dot.coding.cli.tui.app import DotTUIApp

    reset_permission_manager()
    host = CodingHost(workspace=tmp_path, mode=AgentMode.EDIT)
    app = DotTUIApp(host, system="sys")

    info = {
        "tool_name": "edit_file", "agent_mode": "edit", "source": "mode",
        "reason": "edit mode: file write needs review",
        "args": {"file_path": "app.py"},
        "preview": {
            "kind": "update", "path": "app.py",
            "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-v1\n+v2",
            "truncated": False, "error": "", "lines_added": 1, "lines_removed": 1,
        },
    }

    handler = app._make_approval_handler()

    async with app.run_test(size=(100, 30)) as pilot:
        # y = 接受；diff 容器真实渲染
        task = asyncio.ensure_future(handler(dict(info)))
        await pilot.pause(0.3)
        assert isinstance(app.screen, ModalScreen)
        assert app.screen.query("#review-diff")  # diff 预览区已挂载
        await pilot.press("y")
        await pilot.pause(0.2)
        assert await task is True
        assert host.permission.session_accepts_edits() is False  # y 不开启会话豁免

        # n = 拒绝
        deny_task = asyncio.ensure_future(handler(dict(info)))
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert await deny_task is False

        # a = 接受并开启会话级 accept-edits（弹窗默认绑定 app._accept_session_edits）
        task_a = asyncio.ensure_future(handler(dict(info)))
        await pilot.pause(0.3)
        await pilot.press("a")
        await pilot.pause(0.2)
        assert await task_a is True
        assert host.permission.session_accepts_edits() is True
