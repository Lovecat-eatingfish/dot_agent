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


def _install_gc_log_suppressor() -> None:
    """Suppress asyncio GC finalization ERROR logs from MCP streamable_http generators.

    streamable_http_client 内部有 anyio TaskGroup，被 GC 时 asyncio finalizer
    会在不同 task 里调用 __aexit__，触发 "Attempted to exit cancel scope in a
    different task" 错误。这是 MCP SDK + anyio 的已知问题，不影响功能，
    但会刷屏 ERROR 日志。通过 logging filter 静默吞掉。
    """
    try:
        asyncio_logger = logging.getLogger("asyncio")
        if not any(isinstance(f, _MCPGCFilter) for f in asyncio_logger.filters):
            asyncio_logger.addFilter(_MCPGCFilter())
    except Exception:
        pass


class _MCPGCFilter(logging.Filter):
    """Filter out asyncio GC finalization errors from MCP streamable_http generators."""

    _MESSAGES = (
        "error occurred during closing of asynchronous generator",
        "Attempted to exit cancel scope in a different task",
        "aclose(): asynchronous generator is already running",
        "generator didn't stop after athrow",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(m in msg for m in self._MESSAGES):
            return False
        return True


# 模块加载时安装 filter（只执行一次）
_install_gc_log_suppressor()


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
        self._http_gen: Any = None
        self._sse_gen: Any = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        """建立会话并 initialize（重复调用先关闭旧连接）"""
        from mcp import ClientSession

        await self.close()
        self._http_gen = None
        self._sse_gen = None
        self._stack = contextlib.AsyncExitStack()
        streams = await self._open_streams()
        self._session = await self._stack.enter_async_context(ClientSession(*streams))
        await self._session.initialize()
        logger.info("[mcp] Connected: %s (%s, transport=%s)", self.name, self.url, self.transport)

    async def _open_streams(self) -> tuple[Any, Any]:
        """按 transport 打开读写流；auto = 先 streamable http，失败回退 sse"""
        from mcp.client.sse import sse_client

        if self.transport == "sse":
            return await self._open_sse_streams()
        if self.transport == "http":
            return await self._open_http_streams()
        # auto
        try:
            return await self._open_http_streams()
        except Exception as exc:
            logger.info("[mcp] %s streamable-http failed (%s), falling back to sse", self.name, exc)
            await self.close()
            self._stack = contextlib.AsyncExitStack()
            return await self._open_sse_streams()

    async def _open_sse_streams(self) -> tuple[Any, Any]:
        from mcp.client.sse import sse_client

        gen = sse_client(self.url, headers=self.headers)
        streams = await gen.__aenter__()
        self._sse_gen = gen
        return streams

    async def _open_http_streams(self) -> tuple[Any, Any]:
        from mcp.client.streamable_http import streamable_http_client

        if self.headers:
            from mcp.shared._httpx_utils import create_mcp_http_client
            http_client = create_mcp_http_client(headers=self.headers)
            gen = streamable_http_client(self.url, http_client=http_client)
        else:
            gen = streamable_http_client(self.url)

        # 手动 enter async generator（不通过 AsyncExitStack），
        # 确保 __aenter__ / __aexit__ 在同一个 task 里成对出现，
        # 避免 anyio TaskGroup 检测到跨任务退出 cancel scope 报错。
        streams = await gen.__aenter__()
        self._http_gen = gen
        return streams

    async def close(self) -> None:
        # 关闭 streamable_http / sse 异步生成器（可能在任意 task 被 GC 关闭）
        # 所有异常静默吞掉——追踪/连接清理失败不能影响主流程。
        for attr in ("_http_gen", "_sse_gen"):
            gen = getattr(self, attr)
            setattr(self, attr, None)
            if gen is not None:
                try:
                    await gen.__aexit__(None, None, None)
                except GeneratorExit:
                    pass
                except BaseException:
                    pass  # 静默吞掉 anyio CancelScope / TaskGroup 清理异常

        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:
                pass  # AsyncExitStack 清理失败不影响主流程
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
        """调用远程工具，返回文本结果；连接错误重连一次，协议错误直接返回"""
        try:
            return await self._call_once(name, arguments)
        except Exception as exc:
            err_text = str(exc)
            # MCP 协议错误（-32602 Invalid params 等）：服务端返回了非法数据，
            # 重连无法修复，直接返回错误信息，避免触发 streamable_http_client
            # 清理时跨 task 退出 CancelScope 的连锁崩溃。
            if "MCP error" in err_text and ("-32602" in err_text or "Invalid tools/call result" in err_text):
                logger.warning("[mcp] call %s/%s protocol error (not retrying): %s", self.name, name, err_text)
                return f"[MCP protocol error] {err_text}"
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
