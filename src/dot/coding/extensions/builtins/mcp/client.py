"""
dot.coding.extensions.builtins.mcp.client — MCP 客户端（真实实现）

基于官方 mcp SDK 的 SSE 远程服务器客户端。
配置文件：<workspace>/.dot/mcp.json

{
  "mcpServers": {
    "weather": {"url": "https://host/mcp/xxx/sse"}
  }
}

连接生命周期：connect() 建立 SSE 会话并保持（AsyncExitStack），
call_tool 失败自动重连一次；close() 释放。
发现的工具以 "mcp_<server>_<tool>" 命名绑定为 AgentTool（与内置工具平级）。
"""
from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from dot.agent.tools import AgentTool, AgentToolResult
from dot.ai.types import TextContent

logger = logging.getLogger(__name__)

# Streamable HTTP 的 GET 推流在不支持它的服务器上会 405，SDK 重试 2 次后自动放弃，
# 这是协议允许的正常行为（工具调用走 POST 通道，不受影响）——压掉重连过程的 INFO 刷屏
logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)

MCP_CONFIG_FILE = "mcp.json"


def load_mcp_config(workspace: Path) -> dict[str, dict[str, Any]]:
    """读取 .dot/mcp.json 的 mcpServers 段，返回 {server_name: {"url": ...}}"""
    path = workspace / ".dot" / MCP_CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[mcp] Failed to read %s: %s", path, exc)
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {name: cfg for name, cfg in servers.items() if isinstance(cfg, dict)}


class MCPClient:
    """单个 MCP 服务器客户端（保持长连接，自动重连一次）

    transport:
      "http" — Streamable HTTP（现代服务器，URL 无 /sse 后缀）
      "sse"  — SSE（旧式服务器，URL 以 /sse 结尾）
      "auto" — 先试 http，失败回退 sse（默认）
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        transport: str = "auto",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.transport = transport
        self.headers = headers or {}
        self._session: Any = None
        self._stack: Any = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        """建立会话并 initialize（重复调用先关闭旧连接）"""
        from mcp import ClientSession

        await self.close()
        self._stack = contextlib.AsyncExitStack()
        streams = await self._open_streams()
        self._session = await self._stack.enter_async_context(ClientSession(*streams))
        await self._session.initialize()
        logger.info("[mcp] Connected: %s (%s, transport=%s)", self.name, self.url, self.transport)

    async def _open_streams(self) -> tuple[Any, Any]:
        """按 transport 打开读写流；auto = 先 streamable http，失败回退 sse"""
        from mcp.client.sse import sse_client

        if self.transport == "sse":
            return await self._stack.enter_async_context(sse_client(self.url, headers=self.headers))
        if self.transport == "http":
            return await self._open_http_streams()
        # auto
        try:
            return await self._open_http_streams()
        except Exception as exc:
            logger.info("[mcp] %s streamable-http failed (%s), falling back to sse", self.name, exc)
            await self.close()
            self._stack = contextlib.AsyncExitStack()
            return await self._stack.enter_async_context(sse_client(self.url, headers=self.headers))

    async def _open_http_streams(self) -> tuple[Any, Any]:
        from mcp.client.streamable_http import streamable_http_client

        if self.headers:
            from mcp.shared._httpx_utils import create_mcp_http_client
            http_client = create_mcp_http_client(headers=self.headers)
            return await self._stack.enter_async_context(
                streamable_http_client(self.url, http_client=http_client),
            )
        return await self._stack.enter_async_context(streamable_http_client(self.url))

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:
                logger.debug("[mcp] close %s error: %s", self.name, exc)
        self._session = None
        self._stack = None

    async def _ensure_connected(self) -> None:
        if not self.connected:
            await self.connect()

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出服务器提供的工具：[{name, description, input_schema}]"""
        await self._ensure_connected()
        result = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                # mcp 1.x 用 inputSchema，2.x 用 input_schema
                "input_schema": (
                    getattr(tool, "input_schema", None)
                    or getattr(tool, "inputSchema", None)
                    or {"type": "object", "properties": {}}
                ),
            }
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用远程工具，返回文本结果；失败重连一次再试"""
        try:
            return await self._call_once(name, arguments)
        except Exception as exc:
            logger.warning("[mcp] call %s/%s failed (%s), reconnecting once", self.name, name, exc)
            await self.connect()
            return await self._call_once(name, arguments)

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> str:
        await self._ensure_connected()
        result = await self._session.call_tool(name, arguments or {})
        parts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts) or "(empty result)"

    async def make_agent_tools(self) -> list[AgentTool]:
        """把远程工具绑定为 AgentTool（executor 捕获本地 client 闭包）"""
        tools: list[AgentTool] = []
        for tool in await self.list_tools():
            client = self
            remote_name = tool["name"]

            async def execute(
                tool_call_id: str,
                arguments: dict[str, Any],
                signal: object | None = None,
                on_update: object | None = None,
                _client: MCPClient = client,
                _remote: str = remote_name,
            ) -> AgentToolResult:
                text = await _client.call_tool(_remote, arguments)
                return AgentToolResult(content=[TextContent(text=text)], details={})

            tools.append(AgentTool(
                name=f"mcp_{self.name}_{tool['name']}",
                label=f"mcp:{self.name}/{tool['name']}",
                description=f"[mcp:{self.name}] {tool['description']}".strip(),
                parameters=tool["input_schema"],
                execute_fn=execute,
            ))
        logger.info("[mcp] %s: %d tools bound", self.name, len(tools))
        return tools
