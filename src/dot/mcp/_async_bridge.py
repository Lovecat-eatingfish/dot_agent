"""
Async MCP Bridge — 后台 event loop 中运行官方 MCP SDK

职责：
  - 在独立线程常驻一个 asyncio event loop
  - 在 loop 内创建/管理 SdkClient 实例（封装官方 ClientSession）
  - 对外提供 sync 方法，通过 run_coroutine_threadsafe 提交 async 任务

设计：
  - 所有 MCP Server 连接在 loop 线程内创建（stdio 子进程也在此创建）
  - 同步方法通过 _submit() 提交 coroutine 并阻塞等待结果
  - 线程安全：loop 内资源只在 loop 线程访问
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import AsyncExitStack
from typing import Any

from ..core.log import get_logger
from .client import SdkClient, SdkClientConfig

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 30
_INIT_TIMEOUT = 15


class AsyncMCPBridge:
    """后台 event loop 驱动的 MCP Bridge

    每个实例拥有一个独立 event loop（后台线程），
    所有 SDK 操作在此 loop 内执行。
    """

    def __init__(self, workspace: Any = None) -> None:
        self._workspace = workspace
        self._clients: dict[str, SdkClient] = {}
        self._lock = threading.RLock()

        # 后台 event loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._closed = False

    # ============================================================
    # Event loop 管理
    # ============================================================

    def _run_loop(self) -> None:
        """后台线程：运行 event loop 直到被 stop"""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception:
            pass

    def _submit(self, coro, timeout: int = _DEFAULT_TIMEOUT):
        """提交 coroutine 到 loop 线程并等待结果"""
        if self._closed:
            raise RuntimeError("AsyncMCPBridge is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ============================================================
    # Server 生命周期
    # ============================================================

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
    ) -> bool:
        """注册并连接 MCP Server"""
        with self._lock:
            if name in self._clients:
                old = self._clients.pop(name)
                try:
                    self._submit(old.disconnect())
                except Exception:
                    pass

            # 推断传输类型
            if transport_type is None:
                transport_type = "http" if url else "stdio"

            config: SdkClientConfig = {
                "name": name,
                "transport_type": transport_type,
            }
            if transport_type in ("http", "sse", "streamable-http"):
                if not url:
                    logger.error("MCP server '%s' http transport requires url", name)
                    return False
                config["url"] = url
                config["headers"] = headers or {}
            else:
                if not command:
                    logger.error("MCP server '%s' stdio transport requires command", name)
                    return False
                config["command"] = command
                config["args"] = args or []
                config["env"] = env or {}
                config["cwd"] = cwd

            client = SdkClient(config)
            try:
                self._submit(client.connect(), timeout=_INIT_TIMEOUT)
            except Exception as exc:
                logger.error("MCP server '%s' connect failed: %s", name, exc)
                return False

            self._clients[name] = client
            logger.info(
                "MCP server '%s' registered and connected (%s)",
                name, transport_type,
            )
            return True

    def disconnect(self, name: str) -> None:
        """断开指定 MCP Server"""
        with self._lock:
            client = self._clients.pop(name, None)
            if client is not None:
                try:
                    self._submit(client.disconnect())
                except Exception as exc:
                    logger.debug("disconnect %s failed: %s", name, exc)

    def disconnect_all(self) -> None:
        """断开所有 MCP Server"""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                self._submit(client.disconnect())
            except Exception as exc:
                logger.debug("disconnect_all failed for %s: %s", client.name, exc)

    def list_servers(self) -> list[str]:
        """列出已连接的 Server 名称"""
        with self._lock:
            return list(self._clients.keys())

    # ============================================================
    # 工具操作
    # ============================================================

    def list_tools(self, server: str | None = None) -> list[Any]:
        """列出 MCP 工具"""
        with self._lock:
            if server is not None:
                clients = [self._clients[server]] if server in self._clients else []
            else:
                clients = list(self._clients.values())

        tools: list[Any] = []
        for client in clients:
            try:
                tools.extend(self._submit(client.list_tools()))
            except Exception as exc:
                logger.warning("list_tools for '%s' failed: %s", client.name, exc)
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具

        工具名格式：mcp__{server}__{tool} 或 legacy server:tool
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

        try:
            resp = self._submit(client.call_tool(actual_name, arguments))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        text = _extract_text_from_content(resp.content)
        return {
            "ok": not resp.is_error,
            "content": text,
            "is_error": resp.is_error,
            "server": server_name,
            "tool": actual_name,
        }

    def list_resources(self, server: str | None = None) -> list[Any]:
        """列出 MCP 资源"""
        with self._lock:
            if server is not None:
                clients = [self._clients[server]] if server in self._clients else []
            else:
                clients = list(self._clients.values())

        resources: list[Any] = []
        for client in clients:
            try:
                resources.extend(self._submit(client.list_resources()))
            except Exception as exc:
                logger.debug("list_resources for '%s' failed: %s", client.name, exc)
        return resources

    def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        """读取单个 MCP 资源"""
        with self._lock:
            client = self._clients.get(server)
        if client is None:
            return {"ok": False, "error": f"MCP server '{server}' not connected"}
        try:
            return self._submit(client.read_resource(uri))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ============================================================
    # 关闭
    # ============================================================

    def shutdown(self) -> None:
        """关闭所有连接并停止 event loop"""
        # 先断开连接（_submit 会因 _closed 拒绝提交），再停 loop
        self.disconnect_all()
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ============================================================
# Helpers
# ============================================================

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


def _extract_text_from_content(content: list[Any]) -> str:
    """从 SDK 的 CallToolResultContent 列表中提取文本"""
    parts = []
    for item in content:
        item_type = getattr(item, "type", "")
        if item_type == "text":
            parts.append(getattr(item, "text", ""))
        elif isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)
