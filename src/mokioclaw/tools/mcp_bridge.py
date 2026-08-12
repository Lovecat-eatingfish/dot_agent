"""
MCP 适配层（接口预留）

MCP (Model Context Protocol) 是 Anthropic 提出的标准协议，
用于 AI 模型与外部工具/数据源之间的通信。

当前状态：接口预留，未实现具体连接。
未来可以通过此模块接入支持 MCP 的外部服务。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool


class MCPBridge:
    """MCP 服务桥接器（接口预留）

    未来实现：
    - stdio 传输：连接本地 MCP server 进程
    - SSE 传输：连接远程 MCP server
    - 自动工具发现：list_tools() → StructuredTool 转换
    """

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, StructuredTool] = {}

    def connect(self, name: str, config: dict[str, Any]) -> None:
        """连接到 MCP server（预留接口）

        Args:
            name: server 唯一名称
            config: 连接配置，如 {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-*"]}
        """
        self._servers[name] = config

    def disconnect(self, name: str) -> None:
        """断开 MCP server 连接"""
        self._servers.pop(name, None)
        for tool_name in list(self._tools.keys()):
            if tool_name.startswith(f"{name}:"):
                del self._tools[tool_name]

    def list_tools(self, server: str | None = None) -> list[StructuredTool]:
        """列出已加载的 MCP 工具（预留接口）

        Args:
            server: server 名称，None 表示所有

        Returns:
            StructuredTool 列表
        """
        if server is not None:
            prefix = f"{server}:"
            return [tool for name, tool in self._tools.items() if name.startswith(prefix)]
        return list(self._tools.values())

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        """调用 MCP 工具（预留接口）"""
        raise NotImplementedError("MCP tool calling not yet implemented")

    def to_langchain_tools(self, server: str | None = None) -> list[StructuredTool]:
        """将 MCP 工具转换为 LangChain StructuredTool（预留接口）"""
        return self.list_tools(server)


# 模块级单例
_default_bridge: MCPBridge | None = None


def get_mcp_bridge() -> MCPBridge:
    """获取默认 MCP 桥接器单例"""
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = MCPBridge()
    return _default_bridge
