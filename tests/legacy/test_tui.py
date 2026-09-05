"""TUI 模块测试（dot 项目）

覆盖：
  - InputPanel 回车提交 / Shift+Enter 换行 / Ctrl+D 提交
  - 斜杠命令本地处理（不进 LLM）
  - 历史输入 ↑/↓ 回溯
  - 模式循环切换
  - 事件渲染（user/assistant/tool/final/error）
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterator

import pytest

from dot.cli.tui.app import DotTUI
from dot.cli.tui.widgets import ChatPanel, InputPanel, LogPanel, StatusBar


# ============================================================
# Fake bridge：不触碰真实 AgentHost / LLM
# ============================================================

class FakeBridge:
    """SessionBridge 桩：记录调用，run_turn 返回可控事件流"""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.host = FakeHost()
        self._mode = "agent"
        self._history: list[str] = []
        self._history_pos = -1
        self._running = False
        self._events = events or []
        self.submitted: list[str] = []
        self.slash_calls: list[str] = []
        self.reset_calls = 0
        self.save_calls = 0

    # --- 模式 ---
    def get_mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def cycle_mode(self, *, forward: bool = True) -> str:
        order = ["agent", "chat", "code"]
        idx = order.index(self._mode)
        step = 1 if forward else -1
        self._mode = order[(idx + step) % len(order)]
        return self._mode

    # --- 工作目录 ---
    def get_workspace(self) -> str:
        return "/fake/workspace"

    # --- 运行状态 ---
    def is_running(self) -> bool:
        return self._running

    def cancel(self) -> bool:
        return True

    # --- 历史 ---
    def add_history(self, text: str) -> None:
        text = text.strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_pos = -1

    def history_prev(self) -> str | None:
        if not self._history:
            return None
        if self._history_pos == -1:
            self._history_pos = len(self._history) - 1
        elif self._history_pos > 0:
            self._history_pos -= 1
        return self._history[self._history_pos]

    def history_next(self) -> str | None:
        if not self._history or self._history_pos < 0:
            return None
        if self._history_pos < len(self._history) - 1:
            self._history_pos += 1
            return self._history[self._history_pos]
        self._history_pos = -1
        return ""

    # --- 会话 ---
    def reset_session(self) -> str:
        self.reset_calls += 1
        return "新会话: fake"

    def save_session(self, name: str | None = None) -> str:
        self.save_calls += 1
        return "fake-session-id"

    # --- 执行 ---
    def run_turn(self, text: str) -> Iterator[dict[str, Any]]:
        self.submitted.append(text)
        self.add_history(text)
        yield from self._events


class FakeHost:
    def get_mcp_status(self) -> dict[str, Any]:
        return {"online": True, "servers": [], "tools": []}

    def get_token_status(self) -> dict[str, Any]:
        return {"water_level": 0}


# ============================================================
# 工具函数
# ============================================================

def _make_app(bridge: FakeBridge | None = None) -> DotTUI:
    return DotTUI(bridge or FakeBridge())


def _run(coro) -> Any:
    return asyncio.run(coro)


def _log_text(widget) -> str:
    """RichLog 内容拼接为纯文本"""
    parts = []
    for strip in getattr(widget, "lines", []):
        parts.append(strip.text)
    return "\n".join(parts)


# ============================================================
# 回车提交 / 换行
# ============================================================

def test_enter_submits_message() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("h", "e", "l", "l", "o")
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert bridge.submitted == ["hello"]
            assert inp.text == ""

    _run(run())


def test_shift_enter_inserts_newline_not_submit() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("a")
            await pilot.press("shift+enter")
            await pilot.press("b")
            await pilot.pause(0.1)
            assert bridge.submitted == []
            assert inp.text == "a\nb"

    _run(run())


def test_ctrl_d_submits_message() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("h", "i")
            await pilot.press("ctrl+d")
            await pilot.pause(0.1)
            assert bridge.submitted == ["hi"]

    _run(run())


def test_empty_input_does_not_submit() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert bridge.submitted == []

    _run(run())


# ============================================================
# 斜杠命令
# ============================================================

def test_slash_command_does_not_call_llm() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("/", "h", "e", "l", "p")
            await pilot.press("enter")
            await pilot.pause(0.1)
            # 斜杠命令不应进入 run_turn（不进 LLM）
            assert bridge.submitted == []
            assert inp.text == ""

    _run(run())


# ============================================================
# 历史输入
# ============================================================

def test_history_up_down_navigation() -> None:
    bridge = FakeBridge()
    bridge.add_history("first")
    bridge.add_history("second")
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("up")
            await pilot.pause(0.05)
            assert inp.text == "second"
            await pilot.press("up")
            await pilot.pause(0.05)
            assert inp.text == "first"
            await pilot.press("down")
            await pilot.pause(0.05)
            assert inp.text == "second"

    _run(run())


# ============================================================
# 模式循环
# ============================================================

def test_tab_cycles_mode() -> None:
    bridge = FakeBridge()
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert bridge.get_mode() == "chat"
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert bridge.get_mode() == "code"

    _run(run())


# ============================================================
# 事件渲染
# ============================================================

def test_render_user_and_assistant_events() -> None:
    bridge = FakeBridge(events=[
        {"kind": "user", "text": "hello"},
        {"kind": "assistant", "text": "hi there"},
        {"kind": "done"},
    ])
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("h", "i")
            await pilot.press("enter")
            await pilot.pause(0.3)
            chat = app.query_one(ChatPanel)
            text = _log_text(chat)
            assert "hello" in text
            assert "hi there" in text

    _run(run())


def test_render_error_event_does_not_crash() -> None:
    bridge = FakeBridge(events=[{"kind": "error", "text": "boom"}])
    app = _make_app(bridge)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(InputPanel)
            inp.focus()
            await pilot.press("x")
            await pilot.press("enter")
            await pilot.pause(0.1)
            chat = app.query_one(ChatPanel)
            assert "boom" in _log_text(chat)

    _run(run())


# ============================================================
# 状态栏
# ============================================================

def test_status_bar_shows_mode_and_workspace() -> None:
    app = _make_app()

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            sb = app.query_one(StatusBar)
            rendered = str(sb.render())
            assert "agent" in rendered
            assert "workspace" in rendered

    _run(run())
