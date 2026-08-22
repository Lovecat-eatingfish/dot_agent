"""
MCP 管理器 — 基于 AsyncMCPBridge（官方 MCP SDK）

职责：
  - 启动时读取 .dot/mcp.json 配置并建立全部连接
  - 对外提供同步 API：list_tools / call_tool / disconnect_all

配置格式（对齐 Claude Code，两级合并，项目级覆盖同名）：

  ~/.dot/mcp.json（全局）
  <workspace>/.dot/mcp.json（项目级）

  {
    "mcpServers": {
      "amap-maps-streamableHTTP": {
        "url": "https://mcp.amap.com/mcp?key=xxx"
      },
      "local-fs": {
        "command": "node",
        "args": ["./mcp-servers/fs/dist/index.js"],
        "env": {"KEY": "VALUE"}
      }
    }
  }

传输推断：
  - 显式 transportType: "stdio" | "sse" | "http" 优先
  - url 以 /sse 结尾 → sse
  - 有 url → streamable-http
  - 有 command → stdio
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..core.log import get_logger
from ..trace import get_tracer
from ._async_bridge import AsyncMCPBridge

logger = get_logger(__name__)


def infer_transport_type(server: dict[str, Any]) -> str:
    """推断单个 server 配置的传输类型"""
    explicit = str(server.get("transportType", "") or "").lower()
    if explicit:
        if explicit in ("http", "streamable-http", "streamable_http"):
            return "http"
        if explicit in ("sse", "stdio"):
            return explicit
    url = str(server.get("url", "") or "")
    if url:
        from urllib.parse import urlparse

        path = urlparse(url).path.rstrip("/")
        if path.endswith("/sse"):
            return "sse"
        return "http"
    return "stdio"


class MCPManager:
    """MCP Server 管理器（同步 API，底层官方 SDK 跑在后台 event loop）"""

    def __init__(self, workspace: Optional[Any] = None, bridge: Optional[AsyncMCPBridge] = None) -> None:
        self._workspace = workspace
        self._bridge = bridge or AsyncMCPBridge(workspace=workspace)
        self._loaded = False

    # ============================================================
    # 配置加载与连接
    # ============================================================

    def load_config_and_connect(self, *, force: bool = False) -> int:
        """读取 .dot/mcp.json 并连接全部 server，返回成功连接数"""
        if self._loaded and not force:
            return sum(1 for _ in self._bridge.list_servers())
        self._loaded = True

        configs = self._load_merged_configs()
        span = get_tracer().start_span(
            "mcp", "mcp_connect",
            tags={"servers": sorted(configs.keys())},
        )
        connected = 0
        for name, server in configs.items():
            transport = infer_transport_type(server)
            ok = self._bridge.register_server(
                name,
                command=server.get("command"),
                args=server.get("args"),
                env=server.get("env"),
                cwd=server.get("cwd"),
                url=server.get("url"),
                headers=server.get("headers"),
                transport_type=transport,
            )
            if ok:
                connected += 1
        span.set_output_summary(f"{connected}/{len(configs)} connected")
        span.finish()
        if configs:
            logger.info("MCP config loaded: %d/%d servers connected", connected, len(configs))
        return connected

    def _load_merged_configs(self) -> dict[str, dict[str, Any]]:
        """合并全局 + 项目级 mcp.json（项目级覆盖同名）"""
        merged: dict[str, dict[str, Any]] = {}
        for path in self._config_paths():
            data = self._read_config_file(path)
            if not data:
                continue
            servers = data.get("mcpServers", data)
            if not isinstance(servers, dict):
                continue
            for name, server in servers.items():
                if isinstance(server, dict):
                    merged[str(name)] = server
        return merged

    def _config_paths(self) -> list[Path]:
        paths = [Path.home() / ".dot" / "mcp.json"]
        ws = self._workspace
        if ws is not None:
            if hasattr(ws, "workspace"):
                ws = ws.workspace
            ws_path = Path(ws) if not isinstance(ws, Path) else ws
            paths.append(ws_path / ".dot" / "mcp.json")
        return paths

    def _read_config_file(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("MCP config %s parse failed: %s", path, exc)
            return None

    # ============================================================
    # 同步 API（委托 bridge）
    # ============================================================

    def register_server(self, name: str, *, url: str | None = None,
                        command: str | None = None, args: list[str] | None = None,
                        env: dict[str, str] | None = None, cwd: str | None = None,
                        headers: dict[str, str] | None = None,
                        transport_type: str | None = None) -> bool:
        """注册并连接单个 MCP Server（编程式入口）"""
        return self._bridge.register_server(
            name, command=command, args=args, env=env, cwd=cwd,
            url=url, headers=headers,
            transport_type=transport_type or (infer_transport_type({
                "url": url, "command": command,
            }) if url or command else None),
        )

    def disconnect(self, name: str) -> None:
        self._bridge.disconnect(name)

    def disconnect_all(self) -> None:
        self._bridge.disconnect_all()

    def shutdown(self) -> None:
        """断开全部连接并停止后台 event loop"""
        try:
            self._bridge.shutdown()
        except Exception as exc:
            logger.debug("mcp shutdown: %s", exc)

    def list_servers(self) -> list[str]:
        return self._bridge.list_servers()

    def list_tools(self, server: str | None = None) -> list[Any]:
        """列出 MCP 工具（返回 MCPToolInfo：name/description/input_schema/server_name）"""
        return self._bridge.list_tools(server)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具（mcp__server__tool 或 server:tool，带链路追踪）"""
        span = get_tracer().start_span(
            "mcp", "call_mcp_tool",
            tags={"tool_name": tool_name},
            input_summary=str(arguments),
        )
        try:
            result = self._bridge.call_tool(tool_name, arguments)
            span.set_output_summary(str(result.get("content", result.get("error", ""))))
            span.set_tag("server", result.get("server", ""))
            span.finish()
            return result
        except BaseException as exc:
            span.finish(exc)
            raise

    def get_bridge(self) -> AsyncMCPBridge:
        """获取底层 bridge（供高级用法）"""
        return self._bridge
