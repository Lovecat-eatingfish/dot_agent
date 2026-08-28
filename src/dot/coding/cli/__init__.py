"""
dot.coding.cli — CLI 入口

使用 typer + rich 构建 CLI，prompt_toolkit 处理输入。
支持斜杠命令（/mode, /skill:name, /reload 等）。
"""
from __future__ import annotations

from .app import app, main
from .config import CLIConfig
from .tui import DotTUI

__all__ = ["app", "main", "CLIConfig", "DotTUI"]
