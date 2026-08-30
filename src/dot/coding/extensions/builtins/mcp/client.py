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
    """单个 MCP 服务器的 SSE 客户端（保持长连接，自动重连一次）"""

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self.url = url
        self._session: Any = None
        self._stack: Any = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        """建立 SSE 会话并 initialize（重复调用先关闭旧连接）"""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        await self.close()
        self._stack = contextlib.AsyncExitStack()
        streams = await self._stack.enter_async_context(sse_client(self.url))
        self._session = await self._stack.enter_async_context(ClientSession(*streams))
        await self._session.initialize()
        logger.info("[mcp] Connected: %s (%s)", self.name, self.url)

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
                "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
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
