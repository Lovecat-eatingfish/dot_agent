"""
dot.coding.extensions.builtins.mcp.manager — McpConnector（远程 MCP 连接管理）

负责读取 .dot/mcp.json、连接各服务器、把远程工具注册进 AgentTool 注册表。
从 CodingHost 拆出，host 只做委托与 harness 工具同步。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dot.agent.tools import AgentTool

from .client import MCPClient, load_mcp_config

logger = logging.getLogger(__name__)


class McpConnector:
    """管理远程 MCP 服务器连接与远程工具注册表"""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, AgentTool] = {}

    @property
    def tools(self) -> dict[str, AgentTool]:
        """已连接服务器的远程工具（name -> AgentTool）"""
        return self._tools

    async def connect(self, workspace: Path) -> str:
        """连接 workspace 下 .dot/mcp.json 里配置的 MCP 服务器，返回报告文本"""
        servers = load_mcp_config(workspace)
        if not servers:
            logger.info("[mcp] no mcp servers configured")
            return "no mcp servers configured"

        reports = []
        for name, cfg in servers.items():
            url = cfg.get("url")
            if not url:
                reports.append(f"{name}: no url, skipped")
                continue
            client = MCPClient(
                name, url,
                transport=cfg.get("transport", "auto"),
                headers=cfg.get("headers") or None,
            )
            try:
                tools = await client.make_agent_tools()
            except Exception as exc:
                logger.warning("[mcp] %s connect failed: %s", name, exc)
                reports.append(f"{name}: connect failed ({exc})")
                continue
            self._clients[name] = client
            for tool in tools:
                self._tools[tool.name] = tool
            reports.append(f"{name}: {len(tools)} tools")

        return "; ".join(reports)

    def list_servers(self) -> list[dict[str, Any]]:
        """已配置服务器的连接状态"""
        return [{"name": c.name, "url": c.url, "connected": c.connected}
                for c in self._clients.values()]
