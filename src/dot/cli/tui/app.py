"""
DotTUI — Textual 全屏 TUI 应用（对标 Claude Code 终端风格）

简洁、无厚重边框、信息密度高、色彩克制。
"""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical

from .. import slash
from ..session_bridge import DisplayEvent, SessionBridge
from .widgets import ChatPanel, InputPanel, InputSubmitted, LogPanel, StatusBar

# 系统提示文案（压缩联动阈值）
_COMPRESS_HINTS = {
    65: "触发轻量冗余清理，清理无效工具记录",
    80: "Token 水位偏高，执行异步结构化归档压缩",
    95: "高危水位，执行同步强制上下文压缩",
}


class DotTUI(App):
    """dot agent TUI 主应用（Claude Code 风格）"""

    CSS = """
    Screen {
        layout: vertical;
        background: #0D1117;
    }
    #main {
        height: 1fr;
        background: #0D1117;
    }
    """

    BINDINGS = [
        Binding("tab", "tab_handler", "切换模式/补全", priority=True),
        Binding("shift+tab", "cycle_mode_reverse", "反向切换模式", priority=True),
        Binding("ctrl+c", "cancel_task", "中断任务", priority=True),
        Binding("ctrl+l", "clear_screen", "清屏", priority=True),
        Binding("ctrl+r", "reset_session", "重置会话", priority=True),
        Binding("ctrl+s", "save_session", "保存会话", priority=True),
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("escape", "close_popups", "关闭弹窗", priority=True),
    ]

    def __init__(self, bridge: SessionBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self._last_water_band = 0

    # ============================================================
    # 布局
    # ============================================================

    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            yield ChatPanel()
            yield LogPanel()
            yield InputPanel()
        yield StatusBar()

    def on_mount(self) -> None:
        self.title = "dot agent"
        self.sub_title = ""
        sb = self.query_one(StatusBar)
        sb.set_mode(self.bridge.get_mode())
        sb.set_status(
            water=0.0,
            workspace=self.bridge.get_workspace(),
            mcp="online" if self.bridge.host.get_mcp_status().get("online") else "offline",
        )
        self.query_one(InputPanel).focus()
        self.set_interval(2.0, self._refresh_status)
        # 启动欢迎
        chat = self.query_one(ChatPanel)
        chat.write(Text("  dot agent", style="bold #58A6FF"))
        chat.write(Text("  输入消息开始对话，/help 查看命令", style="dim #6E7681"))
        chat.write(Text(""))

    # ============================================================
    # 快捷键 actions
    # ============================================================

    def action_tab_handler(self) -> None:
        """Tab：输入以 / 开头 → 命令补全；否则 → 循环切换模式"""
        inp: InputPanel = self.query_one(InputPanel)
        text = inp.get_text()
        if text.lstrip().startswith("/"):
            self._do_complete()
            return
        self.action_cycle_mode(forward=True)

    def action_cycle_mode(self, *, forward: bool = True) -> None:
        new = self.bridge.cycle_mode(forward=forward)
        self.query_one(StatusBar).set_mode(new)
        self.query_one(ChatPanel).add_system(f"mode → {new}", level="info")

    def action_cycle_mode_reverse(self) -> None:
        self.action_cycle_mode(forward=False)

    def action_cancel_task(self) -> None:
        if self.bridge.is_running():
            ok = self.bridge.cancel()
            self.query_one(ChatPanel).add_system(
                "已请求中断..." if ok else "无运行中的任务", level="warn"
            )
        else:
            self.query_one(ChatPanel).add_system("无运行中的任务", level="info")

    def action_clear_screen(self) -> None:
        self.query_one(ChatPanel).clear_chat()

    def action_reset_session(self) -> None:
        if self.bridge.is_running():
            self.query_one(ChatPanel).add_system("任务执行中，无法重置", level="warn")
            return
        info = self.bridge.reset_session()
        self.query_one(ChatPanel).clear_chat()
        self.query_one(ChatPanel).add_system(info, level="info")

    def action_save_session(self) -> None:
        try:
            sid = self.bridge.save_session()
            self.query_one(ChatPanel).add_system(f"会话已保存: {sid}", level="info")
        except Exception as exc:
            self.query_one(ChatPanel).add_system(f"保存失败: {exc}", level="error")

    def action_close_popups(self) -> None:
        self.query_one(InputPanel).focus()

    def action_quit(self) -> None:
        self.exit()

    def on_input_submitted(self, event: InputSubmitted) -> None:
        """InputPanel 的 Enter / Ctrl+D 提交"""
        text = event.text.strip()
        if not text:
            return
        if self.bridge.is_running():
            self.query_one(ChatPanel).add_system("任务执行中，Ctrl+C 中断后再输入", level="warn")
            return
        self.query_one(InputPanel).clear_input()
        if slash.is_slash_input(text):
            self._handle_slash(text)
        else:
            self._run_turn_worker(text)

    # ============================================================
    # 斜杠命令
    # ============================================================

    def _handle_slash(self, text: str) -> None:
        result = slash.execute(self.bridge, text)
        kind = result.kind
        chat = self.query_one(ChatPanel)
        if kind == "message":
            chat.add_system(result.text, level="info")
        elif kind == "toast":
            chat.add_system(result.text, level=result.level)
        elif kind == "clear_screen":
            chat.clear_chat()
        elif kind == "reset_session":
            chat.clear_chat()
            chat.add_system(result.text, level=result.level)
        elif kind == "quit":
            self.exit()

    def _do_complete(self) -> None:
        """Tab 命令补全"""
        inp: InputPanel = self.query_one(InputPanel)
        text = inp.get_text()
        matches = slash.complete(text)
        if not matches:
            return
        if len(matches) == 1:
            inp.set_text(matches[0])
            lines = matches[0].split("\n")
            inp.cursor_location = (len(lines) - 1, len(lines[-1]))
        else:
            self.query_one(ChatPanel).add_system("  ".join(matches), level="info")

    # ============================================================
    # 历史输入（↑/↓，输入框边界时触发）
    # ============================================================

    def on_key(self, event: Any) -> None:
        try:
            focused = self.focused
        except Exception:
            focused = None
        if not isinstance(focused, InputPanel):
            return
        key = getattr(event, "key", "")
        if key == "up":
            row, _ = focused.cursor_location
            if row == 0:
                prev = self.bridge.history_prev()
                if prev is not None:
                    focused.set_text(prev)
                    focused.cursor_location = (0, len(prev))
                event.prevent_default()
                event.stop()
        elif key == "down":
            row, _ = focused.cursor_location
            last = len(focused.text.split("\n")) - 1
            if row >= last:
                nxt = self.bridge.history_next()
                if nxt is not None:
                    focused.set_text(nxt)
                    lines = nxt.split("\n")
                    focused.cursor_location = (len(lines) - 1, len(lines[-1]))
                else:
                    focused.clear_input()
                event.prevent_default()
                event.stop()

    # ============================================================
    # 执行 worker（textual 线程，事件回灌 UI）
    # ============================================================

    @work(thread=True, exclusive=True, name="dot-turn")
    def _run_turn_worker(self, text: str) -> None:
        self.call_from_thread(self._set_running, True)
        try:
            for ev in self.bridge.run_turn(text):
                self.call_from_thread(self._render_event, ev)
        except Exception as exc:
            self.call_from_thread(self._render_event, {"kind": "error", "text": f"worker: {exc}"})
        finally:
            self.call_from_thread(self._set_running, False)
            self.call_from_thread(self._after_turn)

    def _set_running(self, running: bool) -> None:
        try:
            self.query_one(StatusBar).set_running(running)
        except Exception:
            pass

    def _render_event(self, ev: DisplayEvent) -> None:
        chat = self.query_one(ChatPanel)
        log = self.query_one(LogPanel)
        kind = ev.get("kind", "")
        if kind == "user":
            chat.add_user(ev.get("text", ""))
        elif kind == "assistant":
            chat.add_assistant(ev.get("text", ""))
        elif kind == "tool_call":
            chat.add_tool_call(ev.get("name", ""), ev.get("args"))
            log.add_log(f"⚙ {ev.get('name','')} {ev.get('args','')}")
        elif kind == "tool_result":
            chat.add_tool_result(ev.get("name", ""), ev.get("content", ""))
            log.add_log(f"  ↳ [{ev.get('name','')}] {ev.get('content','')[:200]}")
        elif kind == "node":
            chat.add_node(ev.get("node", ""))
        elif kind == "final":
            chat.add_final(ev.get("answer", ""))
        elif kind == "intervention":
            chat.add_intervention(ev.get("reason", ""))
        elif kind == "cancelled":
            chat.add_cancelled()
            log.add_log("任务被用户中断", level="warn")
        elif kind == "error":
            chat.add_error(ev.get("text", ""))
            log.add_log(ev.get("text", ""), level="error")
        elif kind == "done":
            chat.add_separator()
            log.add_log("turn done")
        elif kind == "system":
            chat.add_system(ev.get("text", ""))

    def _after_turn(self) -> None:
        self._refresh_status()
        self.query_one(InputPanel).focus()

    # ============================================================
    # 状态栏轮询 + 压缩联动提示
    # ============================================================

    def _refresh_status(self) -> None:
        try:
            st = self.bridge.host.get_token_status()
            water = float(st.get("water_level", 0))
            self.query_one(StatusBar).set_status(
                water=water,
                workspace=self.bridge.get_workspace(),
                mcp="online" if self.bridge.host.get_mcp_status().get("online") else "offline",
            )
            band = 0 if water < 65 else 1 if water < 80 else 2 if water < 95 else 3
            if band > self._last_water_band and band in (1, 2, 3):
                hint = _COMPRESS_HINTS.get([65, 80, 95][band - 1], "")
                if hint:
                    self.query_one(ChatPanel).add_system(hint, level="warn")
            self._last_water_band = band
        except Exception:
            pass
