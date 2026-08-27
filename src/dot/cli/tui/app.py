"""
Textual TUI — 双栏布局：左侧日志+状态，右侧聊天+输入。

唯一 UI 框架，合并原 repl.py/sidebar.py/renderer.py/keybindings.py 功能。

布局：
  ┌──────────────────────┬────────────────────┐
  │                      │日志滚动区（可滚动）   │
  │主对话区（可滚动）      │                    │
  │                      ├────────────────────┤
  │                      │底部固定状态面板      │
  ├──────────────────────┴────────────────────┤
  │❯ 输入框                                    │
  └───────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.css.query import QueryError
from textual.dom import DOMNode
from textual.message import Message
from textual.widgets import Footer, Header, Input, Log, Static

from ...core.log import get_logger
from .. import slash
from ..session_bridge import DisplayEvent, SessionBridge

logger = get_logger(__name__)


# ============================================================
# 子组件
# ============================================================

class StatusBar(Static):
    """底部固定状态栏"""

    def set_status(self, mode: str, sid: str, water: float,
                   msgs: int, mcp: str) -> None:
        token_color = ("red" if water >= 95 else
                      "yellow" if water >= 80 else "green")
        self.update(
            f"[b]mode:[/b] {mode}  "
            f"[b]session:[/b] {sid}  "
            f"[b]token:[/b] [{token_color}]{water:.0f}%[/{token_color}]  "
            f"[b]msgs:[/b] {msgs}  "
            f"[b]mcp:[/b] [{'green' if mcp == 'online' else 'red'}]{mcp}[/{'green' if mcp == 'online' else 'red'}]"
        )


class DotInput(Input):
    """带历史回溯和 Tab 补全的输入框"""

    class Submitted(Message):
        """用户提交消息"""
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self) -> None:
        super().__init__(
            placeholder="输入消息，/help 查看命令，Ctrl+C 中断任务...",
            interrupt_other_bindings=True,
        )
        # 输入历史
        self._history: list[str] = []
        self._history_pos: int = -1

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = self.value.strip()
        if text:
            self._add_history(text)
        self.post_message(self.Submitted(self.value))
        self.value = ""

    def _add_history(self, text: str) -> None:
        if self._history and self._history[-1] == text:
            return
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


# ============================================================
# 日志捕获
# ============================================================

class TUILogHandler(logging.Handler):
    """Python logging handler → TUI 侧栏日志"""
    def __init__(self, app: "DotTUI") -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        level = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",
            logging.ERROR: "error",
            logging.CRITICAL: "error",
        }.get(record.levelno, "info")
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        src = (record.name or "root")[:12]
        try:
            self._app.call_from_thread(
                self._app._append_log, f"  {ts} {level.upper():5} {src:12} {msg}\n", level
            )
        except Exception:
            pass


# ============================================================
# 主应用
# ============================================================

class DotTUI(App):
    """dot agent 双栏 Textual TUI — 唯一 UI 框架"""

    CSS = """
    /* 整体：grid 两列 */
    Screen {
        layout: grid;
        grid-size: 2 1;
        grid-columns: 1fr 1fr;
        background: #0D1117;
    }

    /* 左侧栏：grid 两行，日志区占满剩余空间，状态面板固定底部 */
    #sidebar {
        layout: grid;
        grid-rows: 1fr auto;
        width: 100%;
        height: 100%;
        background: #161B22;
        border-right: solid #30363D;
    }

    /* 日志区域：可滚动 */
    #log-area {
        height: 100%;
        overflow-y: auto;
        border-bottom: solid #30363D;
        padding: 1 2;
    }

    /* 状态面板：固定高度 */
    #status-bar {
        height: 3;
        background: #0D1117;
        padding: 1 2;
        border-top: solid #30363D;
    }

    /* 右侧聊天区 */
    #chat-area {
        layout: vertical;
        height: 100%;
        background: #0D1117;
    }

    /* 聊天历史 */
    #chat-history {
        height: 1fr;
        padding: 0 2;
        overflow-y: auto;
    }

    /* 输入区 */
    #input-area {
        height: auto;
        min-height: 3;
        background: #161B22;
        padding: 1 2;
        border-top: solid #30363D;
    }

    /* 样式 */
    Static.chat-user { color: #C9D1D9; }
    Static.chat-assistant { color: #C9D1D9; }
    Static.chat-system { color: #79C0FF; }
    Static.chat-error { color: #F85149; bold: yes; }
    Static.chat-tool { color: #D29922; }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit", "退出", priority=True),
        Binding("ctrl+c", "cancel", "中断任务", priority=True),
        Binding("ctrl+l", "clear", "清屏", priority=True),
        Binding("ctrl+r", "reset", "重置会话", priority=True),
        Binding("ctrl+s", "save", "保存会话", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "折叠侧栏"),
        Binding("tab", "tab_handler", "补全/切换模式"),
        Binding("shift+tab", "cycle_mode_reverse", "反向切换模式"),
        Binding("up", "history_prev", "上一条历史"),
        Binding("down", "history_next", "下一条历史"),
    ]

    MAX_LOG_LINES = 2000
    AUTO_HIDE_WIDTH = 80

    def __init__(self, bridge: SessionBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self._sidebar_visible = True
        self._pending_user: str | None = None
        self._approval_event: threading.Event | None = None
        self._approval_info: dict[str, Any] = {}
        self._approval_result = False
        self._log_handler: TUILogHandler | None = None

    # ============================================================
    # 生命周期
    # ============================================================

    def compose(self) -> ComposeResult:
        yield Header()

        # 左侧栏
        with Container(id="sidebar"):
            yield Log(id="log-area")
            yield StatusBar(id="status-bar")

        # 右侧聊天区
        with Container(id="chat-area"):
            yield VerticalScroll(id="chat-history")
            yield DotInput(id="chat-input")

        yield Footer()

    def on_mount(self) -> None:
        self.title = "dot agent"
        self.sub_title = ""
        log_area = self.query_one("#log-area", Log)
        log_area.max_lines = self.MAX_LOG_LINES
        self.query_one("#chat-input", DotInput).focus()
        self._show_welcome()
        self._refresh_status()
        self.set_interval(2.0, self._status_loop)

        # 注册日志 handler
        self._log_handler = TUILogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

    def on_unmount(self) -> None:
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)

    # ============================================================
    # 欢迎信息
    # ============================================================

    def _show_welcome(self) -> None:
        try:
            log_area = self.query_one("#log-area", Log)
            log_area.write_line("  dot agent", style="bold #58A6FF")
            log_area.write_line("  输入消息开始对话，/help 查看命令", style="#6E7681")
            log_area.write_line("  Ctrl+B 折叠/展开侧栏，Ctrl+C 中断任务", style="#6E7681")
            log_area.write_line("")
        except QueryError:
            pass

    # ============================================================
    # 状态栏
    # ============================================================

    def _refresh_status(self) -> None:
        try:
            st = self.bridge.host.get_token_status()
            water = float(st.get("water_level", 0))
            mcp = self.bridge.host.get_mcp_status()
            mcp_online = bool(mcp.get("online"))
            session = self.bridge.host.session
            sid = session.session_id if session else "-"
            msgs = len(session.messages) if session and session.messages else 0
            mode = self.bridge.get_mode()
            sb = self.query_one("#status-bar", StatusBar)
            sb.set_status(mode, sid, water, msgs, "online" if mcp_online else "offline")
        except QueryError:
            pass

    def _status_loop(self) -> None:
        self._refresh_status()

    # ============================================================
    # 审批交互（非阻塞）
    # ============================================================

    def make_approval_handler(self) -> Callable[[dict[str, Any]], bool]:
        """TUI 内嵌审批 handler（非阻塞模式）"""
        def handler(info: dict[str, Any]) -> bool:
            self._approval_info = info
            self._approval_result = False
            self._approval_event = threading.Event()

            tool = info.get("tool_name", "?")
            args = info.get("args", {})
            source = info.get("source", "")
            reason = info.get("reason", "")
            mode = info.get("agent_mode", "")

            try:
                log_area = self.query_one("#log-area", Log)
                log_area.write_line("", style="")
                log_area.write_line("  ⚠ 【权限审批】需人工确认", style="bold #F85149")
                log_area.write_line(f"  工具: {tool}", style="#C9D1D9")
                log_area.write_line(f"  模式: {mode}", style="#C9D1D9")
                log_area.write_line(f"  原因: {source} — {reason}", style="#C9D1D9")
                log_area.write_line(f"  参数: {str(args)[:200]}", style="#6E7681")
                log_area.write_line("  输入 Y 确认 / N 取消", style="bold #58A6FF")
                log_area.scroll_end(animate=False)
            except QueryError:
                pass

            return False  # 非阻塞

        return handler

    def _consume_approval(self, text: str) -> bool:
        """处理审批 Y/N 响应"""
        if self._approval_event is None:
            return False
        if self._approval_event.is_set():
            return False

        answer = text.strip().lower()
        self._approval_result = answer in ("y", "yes")
        self._approval_event.set()

        label = "✔ 已确认" if self._approval_result else "✗ 已取消"
        style = "bold #3FB950" if self._approval_result else "bold #F85149"

        try:
            log_area = self.query_one("#log-area", Log)
            log_area.write_line(f"  {label}", style=style)
            log_area.write_line("", style="")
            log_area.scroll_end(animate=False)
        except QueryError:
            pass

        return True

    # ============================================================
    # 事件渲染
    # ============================================================

    def _handle_event(self, ev: DisplayEvent) -> None:
        """DisplayEvent → Textual Widget 渲染"""
        try:
            log_area = self.query_one("#log-area", Log)
            chat_hist = self.query_one("#chat-history", VerticalScroll)
        except QueryError:
            return

        kind = ev.get("kind", "")

        if kind == "user":
            self._append_chat(chat_hist, "user", f"❯ {ev.get('text', '')}")
            log_area.write_line(f"❯ {ev.get('text', '')}", style="bold #C9D1D9")

        elif kind == "assistant":
            text = ev.get("text", "")
            # 主栏显示完整回复
            for line in text.splitlines():
                if line.strip():
                    self._append_chat(chat_hist, "assistant", line)
            # 侧栏只显示前 3 行摘要
            for line in text.splitlines()[:3]:
                log_area.write_line(line, style="#C9D1D9")
            if len(text.splitlines()) > 3:
                log_area.write_line("  ...", style="#6E7681")

        elif kind == "tool_call":
            name = ev.get("name", "?")
            args = ev.get("args", "")
            log_area.write_line(f"⚙ {name} {args}", style="#D29922")
            self._append_chat(chat_hist, "tool", f"⚙ {name} {args}")

        elif kind == "tool_result":
            content = (ev.get("content", "") or "")[:200]
            log_area.write_line(f"  ↳ {content}", style="#6E7681")

        elif kind == "node":
            log_area.write_line(f"· {ev.get('node', '')}", style="#6E7681")

        elif kind == "final":
            answer = ev.get("answer", "") or ""
            summary = ev.get("text", "") or ""
            passed = ev.get("passed", False)
            if summary:
                label = f"✓ PASSED — {summary}" if passed else (
                    f"■ STOPPED — {summary}" if "STOPPED" in answer
                    else f"✗ FAILED — {summary}"
                )
            else:
                label = "final"
            for line in answer.splitlines()[:5]:
                self._append_chat(chat_hist, "assistant", line)
            if answer:
                log_area.write_line(f"✔ {answer[:80]}", style="bold #3FB950")
            log_area.write_line("─" * 40, style="#30363D")

        elif kind == "error":
            msg = f"✗ {ev.get('text', '')}"
            self._append_chat(chat_hist, "error", msg)
            log_area.write_line(msg, style="bold #F85149")

        elif kind == "cancelled":
            self._append_chat(chat_hist, "error", "✗ 已中断")
            log_area.write_line("✗ 已中断", style="#D29922")

        elif kind == "intervention":
            reason = ev.get("reason", "")
            self._append_chat(chat_hist, "system", f"⚠ 需人工介入: {reason}")
            log_area.write_line(f"⚠ 需人工介入: {reason}", style="bold #D29922")

        elif kind == "done":
            log_area.write_line("─" * 40, style="#30363D")
            self._append_chat(chat_hist, "system", "─" * 40)

        elif kind == "system":
            text = ev.get("text", "")
            self._append_chat(chat_hist, "system", text)
            log_area.write_line(f"  {text}", style="#58A6FF")

        log_area.scroll_end(animate=False)

    def _append_chat(self, container: DOMNode, style: str, text: str) -> None:
        try:
            container.mount(Static(text, classes=f"chat-{style}"))
        except QueryError:
            pass

    # ============================================================
    # 侧栏日志
    # ============================================================

    def _append_log(self, text: str, level: str = "info") -> None:
        """向侧栏日志追加一行（线程安全）"""
        try:
            log_area = self.query_one("#log-area", Log)
            style_map = {
                "debug": "#6E7681",
                "info": "#58A6FF",
                "warn": "#D29922",
                "error": "#F85149",
            }
            log_area.write_line(text, style=style_map.get(level, "#C9D1D9"))
            log_area.scroll_end(animate=False)
        except QueryError:
            pass

    # ============================================================
    # 输入处理
    # ============================================================

    @on(DotInput.Submitted)
    def on_dot_input_submitted(self, event: DotInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # 审批模式
        if self._approval_event is not None:
            self._consume_approval(text)
            return

        if slash.is_slash_input(text):
            self._handle_slash(text)
            return

        if self.bridge.is_running():
            self._toast("任务执行中，Ctrl+C 中断后再输入", "warn")
            return

        self._run_turn(text)

    def _run_turn(self, text: str) -> None:
        """执行一轮对话（后台线程）"""
        self.bridge.add_history(text)

        def worker() -> None:
            try:
                for ev in self.bridge.run_turn(text):
                    self.call_from_thread(self._handle_event, ev)
            except Exception as exc:
                logger.error("run_turn error: %s", exc, exc_info=True)
                self.call_from_thread(
                    self._handle_event, {"kind": "error", "text": str(exc)}
                )
            finally:
                self.call_from_thread(self._on_turn_done)

        t = Thread(target=worker, daemon=True, name="dot-turn")
        t.start()

    def _on_turn_done(self) -> None:
        self._refresh_status()
        try:
            self.query_one("#chat-input", DotInput).focus()
        except QueryError:
            pass

    # ============================================================
    # slash 命令
    # ============================================================

    def _handle_slash(self, text: str) -> None:
        result = slash.execute(self.bridge, text)
        kind = result.kind
        try:
            log_area = self.query_one("#log-area", Log)
            chat_hist = self.query_one("#chat-history", VerticalScroll)
        except QueryError:
            return

        if kind == "message":
            log_area.write_line(result.text, style="#C9D1D9")
            self._append_chat(chat_hist, "system", result.text)
        elif kind == "toast":
            style_map = {"error": "#F85149", "warn": "#D29922", "info": "#58A6FF"}
            self._toast(result.text, result.level)
            log_area.write_line(result.text, style=style_map.get(result.level, "#58A6FF"))
        elif kind == "clear_screen":
            try:
                for child in list(chat_hist.children):
                    child.remove()
            except QueryError:
                pass
            log_area.clear()
            self._show_welcome()
        elif kind == "reset_session":
            try:
                for child in list(chat_hist.children):
                    child.remove()
            except QueryError:
                pass
            log_area.clear()
            self._show_welcome()
            self._append_chat(chat_hist, "system", result.text or "会话已重置")
        elif kind == "quit":
            self.exit()

        log_area.scroll_end(animate=False)

    # ============================================================
    # 快捷键 actions
    # ============================================================

    def action_quit(self) -> None:
        self.exit()

    def action_cancel(self) -> None:
        if self.bridge.is_running():
            self.bridge.cancel()
            self._toast("已请求中断", "info")
        else:
            self._toast("无运行中的任务", "info")

    def action_clear(self) -> None:
        try:
            log_area = self.query_one("#log-area", Log)
            chat_hist = self.query_one("#chat-history", VerticalScroll)
            for child in list(chat_hist.children):
                child.remove()
            log_area.clear()
            self._show_welcome()
        except QueryError:
            pass

    def action_reset(self) -> None:
        if self.bridge.is_running():
            self._toast("任务执行中，无法重置", "warn")
            return
        info = self.bridge.reset_session()
        self.action_clear()
        self._toast(info, "info")

    def action_save(self) -> None:
        try:
            sid = self.bridge.save_session()
            self._toast(f"会话已保存: {sid}", "info")
        except Exception as exc:
            self._toast(f"保存失败: {exc}", "error")

    def action_toggle_sidebar(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Container)
            self._sidebar_visible = not self._sidebar_visible
            sidebar.display = self._sidebar_visible
        except QueryError:
            pass

    def action_tab_handler(self) -> None:
        """Tab 键：/ 开头时补全命令，否则切换模式"""
        self._handle_tab()

    def action_history_prev(self) -> None:
        """↑ 键：回溯历史输入"""
        try:
            inp = self.query_one("#chat-input", DotInput)
            prev = inp.history_prev()
            if prev is not None:
                inp.value = prev
        except QueryError:
            pass

    def action_history_next(self) -> None:
        """↓ 键：前进历史输入"""
        try:
            inp = self.query_one("#chat-input", DotInput)
            nxt = inp.history_next()
            if nxt is not None:
                inp.value = nxt
            else:
                inp.value = ""
        except QueryError:
            pass

    def action_cycle_mode(self) -> None:
        new = self.bridge.cycle_mode(forward=True)
        self._refresh_status()
        self._toast(f"mode → {new}", "info")

    def action_cycle_mode_reverse(self) -> None:
        new = self.bridge.cycle_mode(forward=False)
        self._refresh_status()
        self._toast(f"mode → {new}", "info")

    # ============================================================
    # 工具
    # ============================================================

    def _toast(self, text: str, level: str = "info") -> None:
        """底部提示"""
        try:
            log_area = self.query_one("#log-area", Log)
            style_map = {"error": "#F85149", "warn": "#D29922", "info": "#58A6FF"}
            log_area.write_line(f"  {text}", style=style_map.get(level, "#C9D1D9"))
            log_area.scroll_end(animate=False)
        except QueryError:
            pass

    # ============================================================
    # Tab 补全
    # ============================================================

    def _handle_tab(self) -> None:
        """Tab 键：/ 开头时补全命令，否则切换模式"""
        try:
            inp = self.query_one("#chat-input", DotInput)
            text = inp.value.lstrip()
            if text.startswith("/"):
                matches = slash.complete(text)
                if matches:
                    if len(matches) == 1:
                        inp.value = matches[0]
                    else:
                        self._toast(" ".join(matches), "info")
            else:
                self.action_cycle_mode()
        except QueryError:
            pass
