"""
全局快捷键体系（对齐设计文档 §4）

所有快捷键由 Textual 原生拦截，优先本地处理，不透传给大模型。
本模块集中定义快捷键元数据与帮助文本；实际绑定在 cli.tui.app 中注册。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    """快捷键定义"""
    key: str           # Textual 按键名，如 "tab" / "ctrl+c"
    label: str         # 展示名，如 "Tab"
    action: str        # TUI action 名，如 "cycle_mode"
    description: str


# 对齐设计文档 §4 快捷键表
SHORTCUTS: tuple[Shortcut, ...] = (
    Shortcut("tab", "Tab", "cycle_mode", "正向循环切换模式：agent → chat → code"),
    Shortcut("shift+tab", "Shift+Tab", "cycle_mode_reverse", "反向循环切换模式：code → chat → agent"),
    Shortcut("enter", "Enter", "submit", "提交对话 / 执行命令（输入框内）"),
    Shortcut("ctrl+d", "Ctrl+D", "submit", "提交对话 / 执行命令（输入框内）"),
    Shortcut("ctrl+c", "Ctrl+C", "cancel_task", "终止当前 Agent 任务，不退出终端"),
    Shortcut("ctrl+l", "Ctrl+L", "clear_screen", "清空屏幕展示，保留会话数据"),
    Shortcut("ctrl+r", "Ctrl+R", "reset_session", "重置全新会话"),
    Shortcut("ctrl+s", "Ctrl+S", "save_session", "快速保存当前会话"),
    Shortcut("ctrl+q", "Ctrl+Q", "quit", "退出 TUI 程序"),
    Shortcut("escape", "ESC", "close_popups", "关闭所有弹窗、取消操作、回到主界面"),
    Shortcut("up", "↑", "history_prev", "回溯历史输入（输入框首行时触发）"),
    Shortcut("down", "↓", "history_next", "前进历史输入（输入框末行时触发）"),
)

# Textual BINDINGS 生成（不含 ↑/↓，那两个在 InputPanel 条件拦截）
def textual_bindings() -> list[tuple[str, str, str]]:
    """返回 (keys, action, description) 三元组，供 App.BINDINGS 使用"""
    return [
        (s.key, s.action, s.description)
        for s in SHORTCUTS
        if s.key not in ("up", "down")
    ]


SHORTCUTS_HELP: str = "\n".join(
    f"  {s.label:<14} {s.description}" for s in SHORTCUTS
)
