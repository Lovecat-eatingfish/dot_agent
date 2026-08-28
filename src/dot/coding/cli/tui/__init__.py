"""
dot.coding.cli.tui — TUI 交互模式

基于 prompt_toolkit + rich 的终端 UI。
消费 AgentEvent 流渲染输出。
"""
from __future__ import annotations

from .app import DotTUI

__all__ = ["DotTUI"]
