"""
SdkClient — 官方 MCP SDK 客户端封装（单 Server 连接）

使用官方 mcp 库的 ClientSession，支持三种传输：
  - stdio: stdio_client + StdioServerParameters（本地子进程 server）
  - sse:   sse_client（SSE 远程 server）
  - http:  streamable_http_client（streamable-http 远程 server，如高德 mcp.amap.com/mcp）

注意：官方 SDK 是 async 的，本类的 async 方法由 AsyncMCPBridge
在后台 event loop 线程内调用；对外不暴露同步 API。
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict

from ..core.log import get_logger

logger = get_logger(__name__)

# initialize / 请求默认超时（秒）
INIT_TIMEOUT = 15


class SdkClientConfig(TypedDict, total=False):
    """单个 MCP Server 的连接配置（与 .dot/mcp.json 的 server 段对应）"""
    name: str
    transport_type: str  # "stdio" | "sse" | "http"
    # http / sse
    url: str
    headers: dict[str, str]
    # stdio
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str


@dataclass
class MCPToolInfo:
    """统一的工具元数据（渐进披露层使用，不依赖 SDK 类型）"""
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""


class SdkClient:
    """官方 ClientSession 的单连接封装

    生命周期：connect()（建传输 + 握手）→ list_tools/call_tool/... → disconnect()
    全部为 async 方法，须在同一 event loop 内调用。
    """

    def __init__(self, config: SdkClientConfig) -> None:
        self.config = config
        self.name: str = config.get("name", "")
        self._stack: Optional[AsyncExitStack] = None
        self._session: Any = None
        self.connected = False

    # ============================================================
    # 连接生命周期
    # ============================================================

    async def connect(self) -> None:
        """建立传输 + initialize 握手"""
        import asyncio

        from mcp import ClientSession

        self._stack = AsyncExitStack()
        try:
            streams = await self._stack.enter_async_context(self._open_transport())
            read, write = streams[0], streams[1]
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self._session.initialize(), timeout=INIT_TIMEOUT)
            self.connected = True
            server_info = getattr(self._session, "server_info", None)
            logger.info(
                "MCP server '%s' connected (%s)%s",
                self.name,
                self.config.get("transport_type"),
                f" → {getattr(server_info, 'name', '?')}" if server_info else "",
            )
        except Exception:
            await self._safe_disconnect()
            raise

    def _open_transport(self):
        """按 transport_type 打开官方 SDK 的传输 async context"""
        import httpx

        transport = (self.config.get("transport_type") or "").lower()
        if transport in ("http", "streamable-http", "streamable_http"):
            from mcp.client.streamable_http import streamable_http_client

            url = self.config.get("url", "")
            if not url:
                raise ValueError(f"MCP server '{self.name}' http transport requires url")
            headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
            # mcp 2.x: headers 通过自定义 httpx client 传入
            http_client = httpx.AsyncClient(headers=headers) if headers else None
            return streamable_http_client(url, http_client=http_client)

        if transport == "sse":
            from mcp.client.sse import sse_client

            url = self.config.get("url", "")
            if not url:
                raise ValueError(f"MCP server '{self.name}' sse transport requires url")
            headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
            return sse_client(url, headers=headers or None, timeout=60)

        # 默认 stdio
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = self.config.get("command", "")
        if not command:
            raise ValueError(f"MCP server '{self.name}' stdio transport requires command")
        params = StdioServerParameters(
            command=command,
            args=list(self.config.get("args") or []),
            env={str(k): str(v) for k, v in (self.config.get("env") or {}).items()} or None,
            cwd=self.config.get("cwd") or None,
        )
        return stdio_client(params)

    async def disconnect(self) -> None:
        """关闭传输与 session"""
        self.connected = False
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.debug("MCP server '%s' disconnect: %s", self.name, exc)

    async def _safe_disconnect(self) -> None:
        try:
            await self.disconnect()
        except Exception:
            pass

    # ============================================================
    # 工具操作
    # ============================================================

    async def list_tools(self) -> list[MCPToolInfo]:
        """拉取 server 的工具列表（转换为统一元数据）

        注意 mcp 2.x 的 Tool 字段是 snake_case（input_schema）。
        """
        if not self.connected or self._session is None:
            return []
        resp = await self._session.list_tools()
        return [
            MCPToolInfo(
                name=tool.name or "",
                description=tool.description or "",
                input_schema=dict(getattr(tool, "input_schema", None) or {}),
                server_name=self.name,
            )
            for tool in (getattr(resp, "tools", None) or [])
            if tool.name
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]):
        """调用工具，返回官方 CallToolResult"""
        if not self.connected or self._session is None:
            raise RuntimeError(f"MCP server '{self.name}' not connected")
        return await self._session.call_tool(tool_name, arguments=arguments or {})

    # ============================================================
    # 资源操作
    # ============================================================

    async def list_resources(self) -> list[Any]:
        if not self.connected or self._session is None:
            return []
        resp = await self._session.list_resources()
        return list(getattr(resp, "resources", None) or [])

    async def read_resource(self, uri: str) -> dict[str, Any]:
        if not self.connected or self._session is None:
            return {"ok": False, "error": "not connected"}
        try:
            resp = await self._session.read_resource(uri)
            return {"ok": True, "contents": _safe_contents(resp)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _safe_contents(resp: Any) -> list[Any]:
    contents = getattr(resp, "contents", None) or []
    result = []
    for item in contents:
        try:
            result.append({
                "uri": str(getattr(item, "uri", "")),
                "mime_type": str(getattr(item, "mimeType", "") or ""),
                "text": getattr(item, "text", None),
            })
        except Exception:
            continue
    return result
