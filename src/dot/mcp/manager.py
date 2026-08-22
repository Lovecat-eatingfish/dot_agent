"""
MCP 管理器

负责 MCP Server 的生命周期管理：注册、连接、断开、工具发现。
对外提供统一接口，供 Session 在初始化时加载 MCP 工具。
"""
from __future__ import annotations

from typing import Any, Optional

from ..core.log import get_logger
from .bridge import MCPBridge

logger = get_logger(__name__)


class MCPManager:
    """MCP Server 管理器

    管理多个 MCP Server 连接，提供统一的工具接口。
    每个 Session 可以拥有独立的 MCPBridge 实例。
    """

    def __init__(self, workspace: Optional[Any] = None) -> None:
        self._bridge = MCPBridge(workspace=workspace)

    def register_server(
        self,
        name: str,
        *,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        transport_type: Optional[str] = None,
        sandbox: Optional[Any] = None,
    ) -> bool:
        """注册并连接 MCP Server"""
        return self._bridge.register_server(
            name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            url=url,
            headers=headers,
            transport_type=transport_type,
            sandbox=sandbox,
        )

    def disconnect(self, name: str) -> None:
        """断开 MCP Server"""
        self._bridge.disconnect(name)

    def disconnect_all(self) -> None:
        """断开所有 MCP Server"""
        self._bridge.disconnect_all()

    def list_servers(self) -> list[str]:
        """列出已连接的 Server 名称"""
        return self._bridge.list_servers()

    def list_tools(self, server: Optional[str] = None) -> list[Any]:
        """列出 MCP 工具"""
        return self._bridge.list_tools(server)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具"""
        return self._bridge.call_tool(tool_name, arguments)

    def to_langchain_tools(self, server: Optional[str] = None) -> list[Any]:
        """将 MCP 工具转换为 LangChain StructuredTool"""
        return self._bridge.to_langchain_tools(server)

    def get_bridge(self) -> MCPBridge:
        """获取底层 MCPBridge 实例（供高级用法）"""
        return self._bridge
