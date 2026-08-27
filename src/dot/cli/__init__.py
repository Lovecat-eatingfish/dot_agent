r"""
dot CLI 终端交互模块（Textual TUI，对标 Claude Code）

分层：
  1. 命令入口层 — cli.app：Typer 子命令（run）
  2. TUI 交互层 — cli.tui.app：Textual 双栏布局、快捷键、斜杠命令
  3. 会话桥接层 — cli.session_bridge：CLI ↔ Agent Graph 适配
  4. 配置层     — cli.config：.env + yaml 配置加载

核心隔离原则：CLI 只做「交互与调度」，不实现任何 Agent 核心业务逻辑。
"""
from __future__ import annotations

from .config import CLIConfig
from .modes import RUN_MODES, RunMode, cycle_mode, mode_label, mode_desc
from .session_bridge import DisplayEvent, SessionBridge

__all__ = [
    "CLIConfig",
    "RUN_MODES",
    "RunMode",
    "cycle_mode",
    "mode_label",
    "mode_desc",
    "DisplayEvent",
    "SessionBridge",
]
