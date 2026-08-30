"""
dot.coding.cli.tui — TUI 交互模式（Textual）

使用 TuiState + TuiEventAdapter 纯数据层 + Textual 渲染。
"""
from __future__ import annotations

from .adapter import TuiEventAdapter
from .app import DotTUI, DotTUIApp, PermissionModal, PromptInput
from .state import ChatItem, TuiState

__all__ = [
    "DotTUI",
    "DotTUIApp",
    "PermissionModal",
    "PromptInput",
    "TuiState",
    "ChatItem",
    "TuiEventAdapter",
]
