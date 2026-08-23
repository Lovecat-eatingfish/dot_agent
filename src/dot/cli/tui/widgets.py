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
# 色彩体系（Claude Code 风格）
# ============================================================

_C = {
    "bg":           "#0D1117",   # 主背景
    "bg_panel":     "#161B22",   # 面板背景
    "bg_status":    "#010409",   # 状态栏背景
    "border":       "#30363D",   # 边框/分隔线
    "border_focus": "#58A6FF",   # 焦点边框
    "accent":       "#58A6FF",   # 强调色
    "user":         "#79C0FF",   # 用户消息
    "assistant":    "#E6EDF3",   # AI 消息
    "tool":         "#D29922",   # 工具调用
    "tool_dim":     "#8B949E",   # 工具参数
    "success":      "#3FB950",   # 成功/运行中
    "warn":         "#D29922",   # 警告
    "error":        "#F85149",   # 错误
    "muted":        "#484F58",   # 次要信息
    "text":         "#C9D1D9",   # 正文
    "text_dim":     "#6E7681",   # 暗文字
    "separator":    "#21262D",   # 分隔线
}


# ============================================================
# 主聊天面板
# ============================================================

class ChatPanel(RichLog):
    """主聊天面板（Claude Code 风格：无边框，简洁滚动，消息前缀区分身份）"""

    DEFAULT_CSS = """
    ChatPanel {
        background: #0D1117;
        height: 1fr;
        min-height: 8;
        padding: 0 2;
        scrollbar-color: #30363D;
        scrollbar-color-hover: #484F58;
        scrollbar-color-active: #58A6FF;
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="chat", markup=True, wrap=True, auto_scroll=True)

    def add_user(self, text: str) -> None:
        self.write(Text(""))
        self.write(Text(f"❯ {text}", style=f"bold {_C['user']}"))

    def add_assistant(self, text: str) -> None:
        self.write(Text(""))
        # 先写一个淡色前缀标记
        self.write(Text("✦", style=_C["accent"]))
        try:
            self.write(Markdown(text))
        except Exception:
            self.write(Text(text, style=_C["assistant"]))

    def add_tool_call(self, name: str, args: Any) -> None:
        args_str = _format_args(args)
        self.write(Text(f"  ⚙ {name}", style=_C["tool"]))
        if args_str:
            self.write(Text(f"    {args_str}", style=f"dim {_C['tool_dim']}"))

    def add_tool_result(self, name: str, content: str) -> None:
        preview = content if len(content) <= 400 else content[:397] + "..."
        lines = preview.split("\n")[:3]
        for line in lines:
            self.write(Text(f"  ┊ {line}", style=f"dim {_C['text_dim']}"))
        remaining = len(preview.split("\n")) - 3
        if remaining > 0:
            self.write(Text(f"  ┊ ... +{remaining} lines", style=f"dim {_C['muted']}"))

    def add_system(self, text: str, *, level: str = "info") -> None:
        style = {
            "info":  f"dim {_C['text_dim']}",
            "warn":  f"bold {_C['warn']}",
            "error": f"bold {_C['error']}",
        }.get(level, f"dim {_C['text_dim']}")
        self.write(Text(f"  ┈ {text}", style=style))

    def add_node(self, node: str) -> None:
        self.write(Text(f"  · {node}", style=f"dim {_C['muted']} italic"))

    def add_final(self, answer: str) -> None:
        self.write(Text(""))
        width = 48
        self.write(Text("═" * width, style=_C["success"]))
        try:
            self.write(Markdown(answer))
        except Exception:
            self.write(Text(answer, style=_C["assistant"]))
        self.write(Text("═" * width, style=_C["success"]))

    def add_intervention(self, reason: str) -> None:
        self.write(Text(""))
        self.write(Text(f"  ⚠  需人工介入: {reason}", style=f"bold {_C['warn']}"))
        self.write(Text("     输入 continue 重新规划 / stop 结束", style=f"dim {_C['text_dim']}"))

    def add_cancelled(self) -> None:
        self.write(Text(f"  ✗ 任务已中断", style=f"bold {_C['error']}"))

    def add_error(self, text: str) -> None:
        self.write(Text(f"  ✗ {text}", style=f"bold {_C['error']}"))

    def add_separator(self) -> None:
        self.write(Text("  ─", style=_C["separator"]))

    def clear_chat(self) -> None:
        self.clear()


# ============================================================
# 日志/事件面板（可折叠）
# ============================================================

class LogPanel(RichLog):
    """日志/事件面板：常驻显示，自动追加滚动，不可聚焦"""

    DEFAULT_CSS = """
    LogPanel {
        background: #0D1117;
        height: 10;
        min-height: 3;
        padding: 0 2;
        border-top: heavy #21262D;
        scrollbar-color: #30363D;
        scrollbar-color-hover: #484F58;
        scrollbar-color-active: #58A6FF;
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="log", markup=True, wrap=True, auto_scroll=True)
        self._log_count = 0
        self.can_focus = False

    def add_log(self, text: str, *, level: str = "info") -> None:
        style = {
            "info":  f"dim {_C['text_dim']}",
            "warn":  _C["warn"],
            "error": _C["error"],
        }.get(level, f"dim {_C['text_dim']}")
        self._log_count += 1
        prefix = f"[dim {_C['muted']}]{self._log_count:>4}[/] "
        self.write(Text.from_markup(f"{prefix}{text}", style=style))


# ============================================================
# 底部输入面板
# ============================================================

class InputPanel(TextArea):
    """输入面板（Claude Code 风格：简洁单行提示符，焦点高亮）"""

    DEFAULT_CSS = """
    InputPanel {
        background: #0D1117;
        height: auto;
        max-height: 12;
        min-height: 3;
        padding: 0 2;
        border-top: solid #21262D;
    }
    InputPanel:focus {
        border-top: solid #58A6FF;
        background: #0D1117;
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
    """状态栏（极简一行：模式 │ 状态 │ Token │ 目录 │ MCP）"""

    DEFAULT_CSS = """
    StatusBar {
        background: #010409;
        color: #8B949E;
        height: 1;
        dock: bottom;
        padding: 0 2;
    }
    """

    _SEP = "[#21262D]│[/]"

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
        sep = self._SEP
        # 模式
        mode_tag = f"[bold {_C['accent']}]{self._mode}[/]"
        # 运行状态
        if self._running:
            run_tag = f"[{_C['success']}]● running[/]"
        else:
            run_tag = f"[{_C['muted']}]○ idle[/]"
        # Token 水位
        w = self._water
        if w < 60:
            wstyle = _C["success"]
        elif w < 85:
            wstyle = _C["warn"]
        else:
            wstyle = f"bold {_C['error']}"
        water_tag = f"[{wstyle}]{w:.0f}%[/]"
        # 工作目录（只取最后一级目录名）
        ws = self._workspace
        ws_short = ws.split("\\")[-1].split("/")[-1] if ws else ""
        ws_tag = f"[{_C['text_dim']}]{ws_short}[/]"
        # MCP
        mcp_color = _C["success"] if self._mcp == "online" else _C["error"]
        mcp_tag = f"[{mcp_color}]mcp:{self._mcp}[/]"
        self.update(
            f" {mode_tag} {sep} {run_tag} {sep} tok:{water_tag} {sep} {ws_tag} {sep} {mcp_tag} "
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
