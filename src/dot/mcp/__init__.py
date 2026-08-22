"""
dot.mcp — MCP (Model Context Protocol) 协议栈 + 渐进披露

  - protocol:   JSON-RPC 消息格式、协议常量、数据结构
  - transport:  传输层（stdio 子进程 + HTTP/SSE）
  - client:     单 Server 连接（initialize 握手、工具发现、调用）
  - sandbox:    MCP 调用沙箱策略
  - bridge:     多 Server 桥接管理器 + LangChain 集成
  - disclosure: 渐进披露（目录注入 + LoadMcpTool 按需加载）
  - manager:    MCPManager（bridge 的生命周期包装）
  - host:       MCPHost / MCPToolExecutor（mcp_ 前缀映射 + 运行时拦截）
"""
from __future__ import annotations

from .bridge import MCPBridge, get_mcp_bridge, reset_mcp_bridge
from .client import MCPClient
from .disclosure import (
    build_load_mcp_tool,
    build_mcp_catalog_text,
    select_mcp_tools_for_bind,
    should_defer_mcp_schemas,
)
from .host import MCPHost, MCPToolExecutor
from .manager import MCPManager
from .protocol import MCPResource, MCPTool, MCPToolResult
from .sandbox import SandboxPolicy, workspace_policy

__all__ = [
    "MCPBridge",
    "get_mcp_bridge",
    "reset_mcp_bridge",
    "MCPClient",
    "build_load_mcp_tool",
    "build_mcp_catalog_text",
    "select_mcp_tools_for_bind",
    "should_defer_mcp_schemas",
    "MCPHost",
    "MCPToolExecutor",
    "MCPManager",
    "MCPResource",
    "MCPTool",
    "MCPToolResult",
    "SandboxPolicy",
    "workspace_policy",
]
