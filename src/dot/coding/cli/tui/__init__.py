"""
dot.coding.cli.tui — TUI 交互模式（Textual）

使用 TuiState + TuiEventAdapter 纯数据层 + Textual 渲染。
"""
from __future__ import annotations

from .adapter import TuiEventAdapter
from .app import DotTUI, DotTUIApp, PermissionModal, PromptInput, WorkflowInterventionModal
from .state import ChatItem, TuiState
from .widgets import QueueStatus

__all__ = [
    "DotTUI",
    "DotTUIApp",
    "PermissionModal",
    "WorkflowInterventionModal",
    "PromptInput",
    "TuiState",
    "ChatItem",
    "TuiEventAdapter",
    "QueueStatus",
]
