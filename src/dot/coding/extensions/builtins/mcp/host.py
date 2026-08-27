"""
dot.coding.extensions.builtins.mcp.host — MCPHost MCP 连接管理

MCP 工具直接绑定为 AgentTool，和内置工具平级（不用渐进披露）。
"""
from __future__ import annotations

import logging
from typing import Any

from dot.agent.tools import AgentTool

logger = logging.getLogger(__name__)


class MCPHost:
    """MCP 主机管理器"""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConnection] = {}
        self._tools: dict[str, AgentTool] = {}

    def add_server(self, name: str, config: dict[str, Any]) -> None:
        """添加 MCP 服务器配置"""
        self._servers[name] = MCPServerConnection(name=name, config=config)
        logger.info("[mcp] Added server: %s", name)

    def get_all_tools(self) -> list[AgentTool]:
        """获取所有 MCP 工具"""
        return list(self._tools.values())

    def get_all_tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_servers(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "status": s.status} for s in self._servers.values()]

    def close(self) -> None:
        """关闭所有连接"""
        for server in self._servers.values():
            server.close()
        self._servers.clear()
        self._tools.clear()


class MCPServerConnection:
    """单个 MCP 服务器连接"""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.status = "disconnected"

    def close(self) -> None:
        self.status = "disconnected"
