"""
TUI 面板组件（对标 Claude Code 终端风格）

  - ChatPanel     主聊天面板：简洁滚动，无厚重边框
  - LogPanel      日志/事件面板（可折叠）
  - InputPanel    底部输入：单行提示符风格，支持多行
  - StatusBar     常驻底部状态栏：极简单行
"""
from __future__ import annotations

import json
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import RichLog, Static, TextArea


# ============================================================
# 主聊天面板
# ============================================================

class ChatPanel(RichLog):
    """主聊天面板（Claude Code 风格：无边框，简洁滚动）"""

    DEFAULT_CSS = """
    ChatPanel {
        background: $surface;
        height: 1fr;
        min-height: 8;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="chat", markup=True, wrap=True, auto_scroll=True)

    def add_user(self, text: str) -> None:
        self.write(Text(""))
        self.write(Text(f"❯ {text}", style="bold #00D4FF"))

    def add_assistant(self, text: str) -> None:
        self.write(Text(""))
        try:
            self.write(Markdown(text))
        except Exception:
            self.write(Text(text, style="#E0E0E0"))

    def add_tool_call(self, name: str, args: Any) -> None:
        args_str = _format_args(args)
        self.write(Text(f"  ⚙ {name}", style="#FFA500"))
        if args_str:
            self.write(Text(f"    {args_str}", style="dim #888888"))

    def add_tool_result(self, name: str, content: str) -> None:
        preview = content if len(content) <= 400 else content[:397] + "..."
        for line in preview.split("\n")[:8]:
            self.write(Text(f"    │ {line}", style="dim #666666"))

    def add_system(self, text: str, *, level: str = "info") -> None:
        style = {"info": "dim #888888", "warn": "#FFCC00", "error": "bold #FF4444"}.get(level, "dim #888888")
        self.write(Text(f"  ╌ {text}", style=style))

    def add_node(self, node: str) -> None:
        self.write(Text(f"  · {node}", style="dim #555555 italic"))

    def add_final(self, answer: str) -> None:
        self.write(Text(""))
        self.write(Text("─── Result ───────────────────────────────────", style="#33AA33"))
        try:
            self.write(Markdown(answer))
        except Exception:
            self.write(Text(answer, style="#E0E0E0"))
        self.write(Text("──────────────────────────────────────────────", style="#33AA33"))

    def add_intervention(self, reason: str) -> None:
        self.write(Text(""))
        self.write(Text(f"  ⚠  需人工介入: {reason}", style="bold #FFCC00"))
        self.write(Text("     输入 continue 重新规划 / stop 结束", style="dim #888888"))

    def add_cancelled(self) -> None:
        self.write(Text("  ✗ 任务已中断", style="bold #FF4444"))

    def add_error(self, text: str) -> None:
        self.write(Text(f"  ✗ {text}", style="bold #FF4444"))

    def add_separator(self) -> None:
        self.write(Text("  ─", style="dim #333333"))

    def clear_chat(self) -> None:
        self.clear()


# ============================================================
# 日志/事件面板（可折叠）
# ============================================================

class LogPanel(RichLog):
    """日志/事件面板：压缩触发、Hook 事件、MCP 日志、错误信息"""

    DEFAULT_CSS = """
    LogPanel {
        background: $surface;
        height: 8;
        min-height: 3;
        padding: 0 1;
        border-top: solid #333333;
    }
    LogPanel.--collapsed {
        height: 0;
        min-height: 0;
        border-top: none;
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="log", markup=True, wrap=True, auto_scroll=True)
        self._collapsed = False

    def add_log(self, text: str, *, level: str = "info") -> None:
        style = {"info": "dim #666666", "warn": "#FFCC00", "error": "#FF4444"}.get(level, "dim #666666")
        self.write(Text(text, style=style))

    def toggle(self) -> bool:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.add_class("--collapsed")
        else:
            self.remove_class("--collapsed")
        return self._collapsed


# ============================================================
# 底部输入面板
# ============================================================

class InputPanel(TextArea):
    """输入面板（Claude Code 风格：简洁单行提示符）"""

    DEFAULT_CSS = """
    InputPanel {
        background: $surface;
        height: auto;
        max-height: 12;
        min-height: 3;
        padding: 0 1;
        border-top: solid #333333;
    }
    InputPanel:focus {
        border-top: solid #00D4FF;
    }
    """

    BINDINGS = [
        Binding("enter", "submit", "提交", priority=True),
        Binding("shift+enter", "newline", "换行", priority=True),
        Binding("ctrl+d", "submit", "提交", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(id="input", language=None)

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def clear_input(self) -> None:
        self.text = ""
        self.cursor_location = (0, 0)

    def action_submit(self) -> None:
        self.post_message(InputSubmitted(self.text))

    def action_newline(self) -> None:
        """Shift+Enter 插入换行"""
        self.insert("\n")


class InputSubmitted(Message):
    """Custom event: user pressed Enter / Ctrl+D to submit input"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


# ============================================================
# 状态栏（常驻底部）
# ============================================================

class StatusBar(Static):
    """状态栏（极简一行：模式 · 状态 · Token · 目录 · MCP）"""

    DEFAULT_CSS = """
    StatusBar {
        background: #1A1A2E;
        color: #888888;
        height: 1;
        dock: bottom;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="statusbar")
        self._mode = "agent"
        self._running = False
        self._water = 0.0
        self._workspace = ""
        self._mcp = "offline"
        self._refresh()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._refresh()

    def set_status(self, *, water: float, workspace: str, mcp: str) -> None:
        self._water = water
        self._workspace = workspace
        self._mcp = mcp
        self._refresh()

    def _refresh(self) -> None:
        # 模式
        mode_tag = f"[bold #00D4FF]{self._mode}[/]"
        # 运行状态
        run_tag = "[bold #33AA33]● running[/]" if self._running else "[dim #555555]○ idle[/]"
        # Token 水位
        w = self._water
        if w < 60:
            wstyle = "#33AA33"
        elif w < 85:
            wstyle = "#FFCC00"
        else:
            wstyle = "bold #FF4444"
        water_tag = f"[{wstyle}]{w:.0f}%[/]"
        # 工作目录（只取最后一级目录名）
        ws = self._workspace
        ws_short = ws.split("\\")[-1].split("/")[-1] if ws else ""
        ws_tag = f"[#666666]{ws_short}[/]"
        # MCP
        mcp_tag = f"[{'#33AA33' if self._mcp == 'online' else '#FF4444'}]mcp:{self._mcp}[/]"
        self.update(
            f" {mode_tag}  {run_tag}   tok:{water_tag}   {ws_tag}   {mcp_tag}"
        )


# ============================================================
# Helpers
# ============================================================

def _format_args(args: Any) -> str:
    """工具参数格式化（截断）"""
    try:
        if isinstance(args, dict):
            text = json.dumps(args, ensure_ascii=False, default=str)
        else:
            text = str(args)
    except Exception:
        text = str(args)
    return text if len(text) <= 120 else text[:117] + "..."
