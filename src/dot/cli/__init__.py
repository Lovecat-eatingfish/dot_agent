r"""
dot CLI & TUI 终端交互模块（对标 Claude Code / CodeX）

分层（对齐设计文档 §2）：
  1. 命令入口层 — cli.app：Typer 子命令（interactive / run / config / mcp）
  2. TUI 交互层  — cli.tui：Textual 多面板界面、快捷键、斜杠命令、弹窗、状态展示
  3. 会话桥接层  — cli.session_bridge：CLI ↔ Agent Graph 适配、会话生命周期、事件订阅
  4. Agent 核心层 — 完全复用 host/graph/session/compress，不侵入核心节点

核心隔离原则：CLI 只做「交互与调度」，不实现任何 Agent 核心业务逻辑。
所有交互能力（快捷键、斜杠命令、模式切换）均为本地前端逻辑，不传入 LLM 上下文。
"""
from __future__ import annotations

from .modes import RUN_MODES, RunMode, cycle_mode, mode_label, mode_desc
from .session_bridge import DisplayEvent, SessionBridge

__all__ = [
    "RUN_MODES",
    "RunMode",
    "cycle_mode",
    "mode_label",
    "mode_desc",
    "DisplayEvent",
    "SessionBridge",
]
