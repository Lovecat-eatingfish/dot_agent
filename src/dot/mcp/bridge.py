"""
MCP 桥接管理器

管理多个 MCP Server 连接，提供工具发现、工具调用和 LangChain 集成。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from ..core.log import get_logger
from .client import MCPClient, _CALL_TIMEOUT, MCPTool
from .protocol import MCPResource, extract_content_parts
from .sandbox import SandboxPolicy, workspace_policy
from .transport import HttpSSETransport, MCPTransport

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
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        transport_type: str | None = None,
        sandbox: SandboxPolicy | None = None,
    ) -> bool:
        """注册并连接 MCP Server

        传输类型（对齐 Claude Code 多传输）：
        - stdio（默认）: 需提供 command + args，启动子进程
        - http/SSE: 需提供 url，POST JSON-RPC + SSE 流读响应

        transport_type 显式指定时可覆盖自动推断（"stdio" | "http"）。

        Args:
            name: server 唯一名称
            command: 可执行文件路径（stdio）
            args: 命令行参数（stdio）
            env: 环境变量（stdio）
            cwd: 工作目录（stdio）
            url: MCP Server HTTP 端点（http/SSE）
            headers: HTTP 请求头（http/SSE）
            transport_type: 显式指定传输类型
            sandbox: 沙箱策略，None 时使用 workspace 默认策略

        Returns:
            是否成功连接
        """
        with self._lock:
            if name in self._clients:
                self.disconnect(name)

            # 推断传输类型：显式 > url 有则 http > 否则 stdio
            if transport_type is None:
                transport_type = "http" if url else "stdio"
            transport_type = transport_type.lower()

            if transport_type in ("http", "sse", "streamable-http"):
                if not url:
                    logger.error("MCP server '%s' http transport requires url", name)
                    return False
                transport = HttpSSETransport(url=url, headers=headers)
            elif transport_type == "stdio":
                if not command:
                    logger.error("MCP server '%s' stdio transport requires command", name)
                    return False
                transport = MCPTransport(command=command, args=args or [], env=env, cwd=cwd)
            else:
                logger.error("MCP server '%s' unknown transport_type: %s", name, transport_type)
                return False

            workspace_path = _get_workspace_path(self._workspace)
            policy = sandbox or (workspace_policy(workspace_path) if workspace_path else None)
            client = MCPClient(name=name, transport=transport, sandbox_policy=policy)

            if client.connect():
                self._clients[name] = client
                self._rebuild_tools()
                logger.info("MCP server '%s' registered and connected (%s)", name, transport_type)
                return True
            else:
                logger.error("MCP server '%s' connection failed", name)
                # connect 失败也要释放 transport：Popen 的子进程和读线程已启动，
                # 不 disconnect 会永久残留（重试注册不断累积进程）
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
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
        """列出 MCP 工具

        锁内仅取 client 快照，阻塞 JSON-RPC 在锁外执行（tools/list 超时 60s），
        避免持锁阻塞其他 bridge 调用。
        """
        with self._lock:
            if server is not None:
                client = self._clients.get(server)
                clients = [client] if client else []
            else:
                clients = list(self._clients.values())
        tools: list[MCPTool] = []
        for client in clients:
            tools.extend(client.list_tools())
        return tools

    def list_resources(self, server: str | None = None) -> list[MCPResource]:
        """列出 MCP 资源（对齐 Claude Code resources 注入）

        同 list_tools：锁内取快照，锁外做阻塞 I/O。
        """
        with self._lock:
            if server is not None:
                client = self._clients.get(server)
                clients = [client] if client else []
            else:
                clients = list(self._clients.values())
        resources: list[MCPResource] = []
        for client in clients:
            resources.extend(client.list_resources())
        return resources

    def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        """读取单个 MCP 资源"""
        with self._lock:
            client = self._clients.get(server)
        if client is None:
            return {"ok": False, "error": f"MCP server '{server}' not connected"}
        return client.read_resource(uri)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具

        工具名格式（对齐 Claude Code）：`mcp__{server}__{tool}`
        兼容旧格式：`server:tool`
        """
        server_name, actual_name = _parse_mcp_tool_name(tool_name)
        if not server_name or not actual_name:
            return {
                "ok": False,
                "error": (
                    f"Invalid MCP tool name: '{tool_name}'. "
                    "Expected 'mcp__server__tool' (or legacy 'server:tool')."
                ),
            }

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
                prefix = f"mcp__{server}__"
                legacy_prefix = f"{server}:"
                return [
                    t for name, t in self._tools.items()
                    if name.startswith(prefix) or name.startswith(legacy_prefix)
                ]
            return list(self._tools.values())

    def to_dict(self) -> dict[str, Any]:
        """导出配置（用于持久化）

        按传输类型导出：stdio 导出 command/args，http 导出 url/headers。
        """
        with self._lock:
            out: dict[str, Any] = {}
            for name, client in self._clients.items():
                transport = client._transport
                if isinstance(transport, HttpSSETransport):
                    out[name] = {"type": "http", "url": transport._url}
                else:
                    out[name] = {
                        "type": "stdio",
                        "command": getattr(transport, "_command", ""),
                        "args": getattr(transport, "_args", []),
                    }
            return out

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
                qualified_name = f"mcp__{mcp_tool.server_name}__{mcp_tool.name}"
                new_tools[qualified_name] = _mcp_tool_to_langchain(mcp_tool, self)

        # 更新工具映射（在锁内）
        with self._lock:
            self._tools = new_tools


def _parse_mcp_tool_name(tool_name: str) -> tuple[str, str]:
    """解析 mcp__server__tool 或 legacy server:tool"""
    if tool_name.startswith("mcp__"):
        rest = tool_name[len("mcp__"):]
        server_name, sep, actual_name = rest.partition("__")
        if sep and server_name and actual_name:
            return server_name, actual_name
        return "", ""
    if ":" in tool_name:
        server_name, _, actual_name = tool_name.partition(":")
        return server_name, actual_name
    return "", ""


def _mcp_tool_to_langchain(mcp_tool: MCPTool, bridge: "MCPBridge") -> StructuredTool:
    """将 MCPTool 转换为 LangChain StructuredTool"""
    qualified_name = f"mcp__{mcp_tool.server_name}__{mcp_tool.name}"

    def _invoke(**kwargs: Any) -> dict[str, Any]:
        # catch-and-return：业务异常不抛出，避免崩掉 agent loop
        try:
            result = bridge.call_tool(qualified_name, kwargs)
            if result.get("ok"):
                return {
                    "ok": True,
                    "content": str(result.get("content", "")),
                    "server": result.get("server"),
                    "tool": result.get("tool"),
                }
            return {
                "ok": False,
                "is_error": True,
                "error": result.get("error", "unknown"),
                "content": f"[MCP Error] {result.get('error', 'unknown')}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "is_error": True,
                "error": f"{type(exc).__name__}: {exc}",
                "content": f"[MCP Error] {type(exc).__name__}: {exc}",
            }

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


# ============================================================
# 配置加载
# ============================================================


def _load_mcp_servers_from_config(bridge: MCPBridge, workspace: Any = None) -> None:
    """从配置文件加载 MCP 服务器

    搜索路径（优先级：后加载的覆盖同名 server）：
    1. ~/.dot/mcp.json（全局用户配置）
    2. <workspace>/.mokioclaw/mcp.json（项目级配置，优先）
    3. <workspace>/.dot/mcp.json（项目级配置，回退）

    配置格式（对齐 Claude Code claude_desktop_config.json）：
    .. code-block:: json

        {
            "mcpServers": {
                "server-name": {
                    "command": "node",
                    "args": ["path/to/server.js"],
                    "env": {"KEY": "VALUE"}
                }
            }
        }

    HTTP/SSE 传输：
    .. code-block:: json

        {
            "mcpServers": {
                "remote-api": {
                    "url": "http://localhost:3000/mcp",
                    "headers": {"Authorization": "Bearer token"}
                }
            }
        }
    """
    config_paths: list[Path] = [Path.home() / ".dot" / "mcp.json"]
    ws_path = _get_workspace_path(workspace)
    if ws_path is not None:
        # 优先 .mokioclaw/mcp.json，回退 .dot/mcp.json
        config_paths.append(ws_path / ".dot" / "mcp.json")

    for config_path in config_paths:
        if not config_path.is_file():
            continue
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("MCP config %s parse failed: %s", config_path, exc)
            continue

        servers = data.get("mcpServers", data)
        if not isinstance(servers, dict):
            continue

        for name, server in servers.items():
            if not isinstance(server, dict):
                continue
            bridge.register_server(
                name,
                command=server.get("command"),
                args=server.get("args"),
                env=server.get("env"),
                cwd=server.get("cwd"),
                url=server.get("url"),
                headers=server.get("headers"),
                transport_type=server.get("transportType"),
            )


# ============================================================
# 模块级单例
# ============================================================

_default_bridge: MCPBridge | None = None
_bridge_lock = threading.Lock()
_servers_loaded: bool = False


def get_mcp_bridge(workspace: Any = None) -> MCPBridge:
    """获取全局 MCP 桥接器单例

    首次创建时自动从配置文件加载 MCP 服务器（仅一次）。
    """
    global _default_bridge, _servers_loaded
    if _default_bridge is None:
        with _bridge_lock:
            if _default_bridge is None:
                _default_bridge = MCPBridge(workspace=workspace)
                if not _servers_loaded:
                    _load_mcp_servers_from_config(_default_bridge, workspace)
                    _servers_loaded = True
    return _default_bridge


def reset_mcp_bridge() -> None:
    """重置全局单例"""
    global _default_bridge, _servers_loaded
    with _bridge_lock:
        if _default_bridge is not None:
            _default_bridge.disconnect_all()
        _default_bridge = None
        _servers_loaded = False
