"""
dot.coding.ui — UI 抽象层

UiBridge Protocol 定义 UI 交互接口。
NullUiBridge 确保 extension 在无 UI 环境中正常运行。
"""
from __future__ import annotations

from .bridge import UiBridge, NullUiBridge, ConsoleUiBridge

__all__ = ["UiBridge", "NullUiBridge", "ConsoleUiBridge"]
