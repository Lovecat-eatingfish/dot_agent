"""
MCP (Model Context Protocol) 桥接层（已实现）

已实现功能：
- stdio 传输：通过 subprocess 与外部 MCP Server 进程通信
- JSON-RPC 2.0：完整的请求/响应/通知消息格式
- 工具发现：tools/list 自动获取 Server 可用工具
- 工具调用：tools/call 经沙箱验证后执行
- 沙箱安全：文件路径白名单/黑名单、命令白名单、网络控制、超时限制
- LangChain 集成：MCP 工具自动转换为 StructuredTool

使用方式：
    from mokioclaw.mcp.bridge import get_mcp_bridge
    bridge = get_mcp_bridge()
    bridge.register_server("fs", command="node", args=["./server/index.js"])
    tools = bridge.to_langchain_tools()
"""
from __future__ import annotations

from mokioclaw.mcp.bridge import MCPBridge, get_mcp_bridge, reset_mcp_bridge

__all__ = ["MCPBridge", "get_mcp_bridge", "reset_mcp_bridge"]
