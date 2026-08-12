"""
MCP 桥接管理器

管理多个 MCP Server 连接，提供工具发现、工具调用和 LangChain 集成。
"""
from __future__ import annotations

import threading
from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger
from mokioclaw.mcp.client import MCPClient, _CALL_TIMEOUT, MCPTool
from mokioclaw.mcp.protocol import extract_content_parts
from mokioclaw.mcp.sandbox import SandboxPolicy, workspace_policy
from mokioclaw.mcp.transport import MCPTransport

logger = get_logger(__name__)


class MCPBridge:
    """MCP 服务桥接管理器

    管理多个 MCP Server 连接，提供统一的工具接口。

    使用方式：
        bridge = MCPBridge(workspace=Path("."))
        bridge.register_server("fs", command="node", args=["./mcp-servers/filesystem/dist/index.js"])
        tools = bridge.to_langchain_tools()
    """

    def __init__(self, workspace: Any = None) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, StructuredTool] = {}
        self._lock = threading.RLock()
        self._workspace = workspace

    def register_server(
        self,
        name: str,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        sandbox: SandboxPolicy | None = None,
    ) -> bool:
        """注册并连接 MCP Server

        Args:
            name: server 唯一名称
            command: 可执行文件路径
            args: 命令行参数
            env: 环境变量
            cwd: 工作目录
            sandbox: 沙箱策略，None 时使用 workspace 默认策略

        Returns:
            是否成功连接
        """
        with self._lock:
            if name in self._clients:
                self.disconnect(name)

            workspace_path = _get_workspace_path(self._workspace)
            policy = sandbox or workspace_policy(workspace_path) if workspace_path else None
            transport = MCPTransport(command=command, args=args or [], env=env, cwd=cwd)
            client = MCPClient(name=name, transport=transport, sandbox_policy=policy)

            if client.connect():
                self._clients[name] = client
                self._rebuild_tools()
                logger.info("MCP server '%s' registered and connected", name)
                return True
            else:
                logger.error("MCP server '%s' connection failed", name)
                return False

    def disconnect(self, name: str) -> None:
        """断开 MCP Server"""
        with self._lock:
            client = self._clients.pop(name, None)
            if client is not None:
                client.disconnect()
                self._rebuild_tools()

    def disconnect_all(self) -> None:
        """断开所有 MCP Server"""
        with self._lock:
            for client in self._clients.values():
                client.disconnect()
            self._clients.clear()
            self._tools.clear()

    def list_servers(self) -> list[str]:
        """列出已连接的 Server 名称"""
        with self._lock:
            return list(self._clients.keys())

    def list_tools(self, server: str | None = None) -> list[MCPTool]:
        """列出 MCP 工具"""
        with self._lock:
            if server is not None:
                client = self._clients.get(server)
                return client.list_tools() if client else []
            tools: list[MCPTool] = []
            for client in self._clients.values():
                tools.extend(client.list_tools())
            return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具

        工具名格式：`server_name:tool_name`
        """
        if ":" not in tool_name:
            return {"ok": False, "error": f"Invalid MCP tool name: '{tool_name}'. Expected 'server:tool'."}

        server_name, _, actual_name = tool_name.partition(":")
        with self._lock:
            client = self._clients.get(server_name)

        if client is None:
            return {"ok": False, "error": f"MCP server '{server_name}' not connected"}

        result = client.call_tool(actual_name, arguments)
        text, _ = extract_content_parts(result)
        return {
            "ok": not result.is_error,
            "content": text,
            "is_error": result.is_error,
            "server": server_name,
            "tool": actual_name,
        }

    def to_langchain_tools(self, server: str | None = None) -> list[StructuredTool]:
        """将 MCP 工具转换为 LangChain StructuredTool"""
        with self._lock:
            if server is not None:
                return [t for name, t in self._tools.items() if name.split(":")[0] == server]
            return list(self._tools.values())

    def to_dict(self) -> dict[str, Any]:
        """导出配置（用于持久化）"""
        with self._lock:
            return {
                name: {
                    "command": client._transport._command,
                    "args": client._transport._args,
                }
                for name, client in self._clients.items()
            }

    def _rebuild_tools(self) -> None:
        """根据当前连接的 Server 重建工具映射

        注意：在锁外执行 list_tools() 以避免阻塞其他操作。
        """
        # 收集 client 列表（在锁内）
        with self._lock:
            clients = list(self._clients.values())

        # 在锁外执行阻塞 I/O
        new_tools: dict[str, StructuredTool] = {}
        for client in clients:
            for mcp_tool in client.list_tools():
                qualified_name = f"{mcp_tool.server_name}:{mcp_tool.name}"
                new_tools[qualified_name] = _mcp_tool_to_langchain(mcp_tool, self)

        # 更新工具映射（在锁内）
        with self._lock:
            self._tools = new_tools


def _mcp_tool_to_langchain(mcp_tool: MCPTool, bridge: "MCPBridge") -> StructuredTool:
    """将 MCPTool 转换为 LangChain StructuredTool"""
    qualified_name = f"{mcp_tool.server_name}:{mcp_tool.name}"

    def _invoke(**kwargs: Any) -> str:
        result = bridge.call_tool(qualified_name, kwargs)
        if result.get("ok"):
            return str(result.get("content", ""))
        return f"[MCP Error] {result.get('error', 'unknown')}"

    return StructuredTool.from_function(
        name=qualified_name,
        func=_invoke,
        description=f"[MCP:{mcp_tool.server_name}] {mcp_tool.description}",
    )


def _get_workspace_path(workspace: Any) -> Any:
    """从 workspace 参数获取 Path 对象"""
    if workspace is None:
        return None
    if hasattr(workspace, "workspace"):
        return workspace.workspace
    if hasattr(workspace, "resolve"):
        return workspace
    return None


# 模块级单例
_default_bridge: MCPBridge | None = None
_bridge_lock = threading.Lock()


def get_mcp_bridge(workspace: Any = None) -> MCPBridge:
    """获取全局 MCP 桥接器单例"""
    global _default_bridge
    if _default_bridge is None:
        with _bridge_lock:
            if _default_bridge is None:
                _default_bridge = MCPBridge(workspace=workspace)
    return _default_bridge


def reset_mcp_bridge() -> None:
    """重置全局单例"""
    global _default_bridge
    with _bridge_lock:
        if _default_bridge is not None:
            _default_bridge.disconnect_all()
        _default_bridge = None
