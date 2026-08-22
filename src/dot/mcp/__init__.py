"""
dot.mcp — MCP 集成（基于官方 MCP SDK，doc/fix-mcp.md）

架构：
  - client:        SdkClient（官方 ClientSession 封装：stdio / sse / streamable-http）
  - _async_bridge: AsyncMCPBridge（后台 event loop 线程驱动 SDK，对外同步 API）
  - manager:       MCPManager（.dot/mcp.json 配置加载 + 连接管理）
  - host:          MCPHost / MCPToolExecutor（渐进披露：目录注入 + 按需加载）
"""
from __future__ import annotations

from ._async_bridge import AsyncMCPBridge
from .client import MCPToolInfo, SdkClient, SdkClientConfig
from .host import MCPHost, MCPToolExecutor
from .manager import MCPManager, infer_transport_type

__all__ = [
    "AsyncMCPBridge",
    "SdkClient",
    "SdkClientConfig",
    "MCPToolInfo",
    "MCPManager",
    "MCPHost",
    "MCPToolExecutor",
    "infer_transport_type",
]
