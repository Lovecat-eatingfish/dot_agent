"""
dot.coding.extensions.builtins — 内置扩展

内置扩展使用 BuiltInExtensionContext，拥有受信任的运行时依赖。
"""
from __future__ import annotations

from .mcp import MCPHost, MCPClient

__all__ = ["MCPHost", "MCPClient"]
