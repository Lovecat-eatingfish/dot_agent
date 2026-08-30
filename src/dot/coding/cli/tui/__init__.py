"""
dot.coding.cli.tui — TUI 交互模式

基于 prompt_toolkit + rich 的终端 UI。
使用 TuiState + TuiEventAdapter 模式消费 AgentEvent。
"""
from __future__ import annotations

from .app import DotTUI
from .state import TuiState, ChatItem
from .adapter import TuiEventAdapter

__all__ = ["DotTUI", "TuiState", "ChatItem", "TuiEventAdapter"]
