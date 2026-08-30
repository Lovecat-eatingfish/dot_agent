"""TUI（Textual）端到端冒烟 + state/adapter 纯逻辑测试"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dot.agent import events as ae
from dot.agent.tools import AgentTool, AgentToolResult
from dot.ai import events as pe
from dot.ai.types import AssistantMessage, TextContent, ToolCall
from dot.coding.cli.tui.adapter import TuiEventAdapter
from dot.coding.cli.tui.app import DotTUIApp, PromptInput
from dot.coding.cli.tui.autocomplete import build_completion_state
from dot.coding.cli.tui.state import TuiState
from dot.coding.cli.tui.widgets import StatusBar, TranscriptView
from dot.coding.commands import get_command_registry


def _assistant(text: str, *, model: str = "m", **kwargs) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], model=model, **kwargs)


# ============================================================
# adapter 纯逻辑
# ============================================================

def test_adapter_streams_text_and_thinking() -> None:
    state = TuiState()
    adapter = TuiEventAdapter(state)
    adapter.apply(ae.AgentStartEvent())
    adapter.apply(ae.MessageStartEvent(message=_assistant("")))
    adapter.apply(ae.MessageUpdateEvent(
        message=_assistant(""),
        provider_event=pe.TextDeltaEvent(content_index=0, delta="Hello", partial=_assistant("")),
    ))
    adapter.apply(ae.MessageUpdateEvent(
        message=_assistant(""),
        provider_event=pe.ThinkingDeltaEvent(content_index=0, delta="hmm", partial=_assistant("")),
    ))
    assert state.assistant_buffer == "Hello"
    assert state.items[-1].role == "thinking"

    adapter.apply(ae.MessageEndEvent(message=_assistant("Hello world")))
    assert state.items[-1].role == "assistant"
    assert state.items[-1].text == "Hello world"
    assert state.assistant_buffer == ""


def test_adapter_batches_consecutive_tool_calls() -> None:
    state = TuiState()
    adapter = TuiEventAdapter(state)
    msg = AssistantMessage(
        content=[
            ToolCall(id="c1", name="read_file", arguments={"file_path": "x"}),
            ToolCall(id="c2", name="bash", arguments={"command": "ls"}),
        ],
        model="m",
        stop_reason="toolUse",
    )
    adapter.apply(ae.MessageEndEvent(message=msg))
    adapter.apply(ae.ToolExecutionStartEvent(tool_call_id="c1", tool_name="read_file", args={}))
    adapter.apply(ae.ToolExecutionStartEvent(tool_call_id="c2", tool_name="bash", args={}))
    first, second = state.find_tool_item("c1"), state.find_tool_item("c2")
    assert first.tool_batch_id is not None
    assert first.tool_batch_id == second.tool_batch_id

    adapter.apply(ae.ToolExecutionEndEvent(
        tool_call_id="c1", tool_name="read_file",
        result=AgentToolResult(content=[TextContent(text="content here")]), is_error=False,
    ))
    assert "OK" in state.find_tool_item("c1").tool_result_text


# ============================================================
# 补全
# ============================================================

def test_completion_matches_slash_commands() -> None:
    registry = get_command_registry()
    completion = build_completion_state("/re", registry)
    assert completion.active
    assert any(value.startswith("/resume") for value, _ in completion.options)
    assert not build_completion_state("hello", registry).active
    assert not build_completion_state("/help me", registry).active


# ============================================================
# Textual pilot 端到端
# ============================================================

def test_tui_app_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    asyncio.run(_run_tui_end_to_end(tmp_path))


async def _run_tui_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")

    from dot.coding.host import CodingHost

    call = ToolCall(id="c1", name="write_tool", arguments={})

    class FakeProvider:
        model = "test-model"
        script = ["tool"]

        async def stream_response(self, *, messages, **kw):
            if self.script and self.script[0] == "tool":
                self.script.pop(0)
                partial = AssistantMessage(
                    content=[ToolCall(id="c1", name="write_tool", arguments={})],
                    model="t", provider="fake", stop_reason="toolUse",
                )
                yield pe.ToolCallEndEvent(content_index=0, tool_call=call, partial=partial)
                yield pe.AssistantDoneEvent(reason="toolUse", message=partial)
            else:
                partial = _assistant("all done", model="t")
                yield pe.AssistantDoneEvent(reason="stop", message=partial)

    async def do_write(tool_call_id, arguments, signal=None, on_update=None):
        (tmp_path / "a.txt").write_text("written-by-tool", encoding="utf-8")
        return AgentToolResult(content=[TextContent(text="file written")], details={})

    host = CodingHost(workspace=tmp_path)
    app = DotTUIApp(host, system="sys")
    harness = app._harness
    harness._config.provider = FakeProvider()
    harness.set_tools([AgentTool(
        name="write_tool", label="write_tool", description="",
        parameters={"type": "object", "properties": {}}, execute_fn=do_write,
    )])

    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(TranscriptView)
        app.query_one(StatusBar)
        prompt = app.query_one("#prompt", PromptInput)

        # 补全：/rew → Tab → /rewind
        prompt.focus()
        prompt.text = "/rew"
        await pilot.pause()
        assert app._completion is not None and app._completion.active
        app.action_accept_completion()
        await pilot.pause()
        assert prompt.text.startswith("/rewind")

        # 斜杠命令
        prompt.text = "/help"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        # 完整 agent 回合：工具执行 + end_turn（增量落盘 + git commit）
        prompt.text = "please write the file"
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.1)
            if not app.state.running:
                break
        assert not app.state.running
        assert (tmp_path / "a.txt").read_text() == "written-by-tool"
        turns = host.list_turns()
        assert len(turns) == 1 and turns[0]["commit"]
        assert len(harness.messages) >= 4

        # 权限弹窗：批准 / 拒绝
        handler = app._make_approval_handler()

        async def ask() -> bool:
            return await handler({"tool_name": "bash", "args": {"command": "ls"}, "reason": "test"})

        task = asyncio.ensure_future(ask())
        await pilot.pause(0.3)
        from textual.screen import ModalScreen

        assert isinstance(app.screen, ModalScreen)
        await pilot.press("y")
        await pilot.pause(0.2)
        assert await task is True

        async def ask_deny() -> bool:
            return await handler({"tool_name": "bash", "args": {}, "reason": "t2"})

        deny_task = asyncio.ensure_future(ask_deny())
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert await deny_task is False

        # Esc 中断（空闲时 no-op 不崩溃）
        await pilot.press("escape")
        await pilot.pause()
