"""
dot.coding.extensions.builtins.mcp — MCP 内置扩展

MCP 工具直接绑定为 AgentTool，和内置工具平级（不用渐进披露）。
"""
from __future__ import annotations

from .host import MCPHost
from .client import MCPClient

__all__ = [
    "MCPHost",
    "MCPClient",
]
