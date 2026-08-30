"""
dot.coding.cli — CLI 入口

使用 typer + rich 构建 CLI。
默认 console 模式（input/print + logging）。
"""
from __future__ import annotations

from .app import app, main
from .config import CLIConfig

__all__ = ["app", "main", "CLIConfig"]
