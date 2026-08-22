"""测试 MCP SDK 集成"""
import pytest

# 1. 官方 SDK 导入可用
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_sdk_imports():
    """官方 MCP SDK 核心组件可导入"""
    assert ClientSession is not None
    assert sse_client is not None
    assert stdio_client is not None
    assert StdioServerParameters is not None


# 2. 项目内部模块导入
def test_project_mcp_modules():
    """项目 MCP 模块可导入（无自研 protocol/transport 依赖）"""
    from dot.mcp import (
        MCPBridge,
        AsyncMCPBridge,
        SdkClient,
        MCPHost,
        MCPToolExecutor,
        MCPManager,
        SandboxPolicy,
        workspace_policy,
        build_mcp_catalog_text,
    )
    assert MCPBridge is not None
    assert AsyncMCPBridge is not None
    assert SdkClient is not None


# 3. SdkClient 配置类型
def test_sdk_client_config():
    """SdkClient 可接受 SSE 和 stdio 配置"""
    from dot.mcp.client import SdkClient

    # SSE 配置
    sse_client_config = {
        "name": "test-sse",
        "transport_type": "http",
        "url": "http://example.com/mcp",
        "headers": {"Authorization": "Bearer token"},
    }
    client = SdkClient(sse_client_config)
    assert client.name == "test-sse"
    assert client._config["url"] == "http://example.com/mcp"

    # stdio 配置
    stdio_client_config = {
        "name": "test-stdio",
        "transport_type": "stdio",
        "command": "node",
        "args": ["server.js"],
        "env": {"KEY": "value"},
    }
    client = SdkClient(stdio_client_config)
    assert client.name == "test-stdio"
    assert client._config["command"] == "node"


# 4. AsyncMCPBridge 生命周期
def test_async_bridge_lifecycle():
    """AsyncMCPBridge 可以创建和关闭"""
    from dot.mcp._async_bridge import AsyncMCPBridge

    bridge = AsyncMCPBridge()
    assert bridge.list_servers() == []
    bridge.shutdown()


# 5. MCPBridge 同步 API
def test_mcp_bridge_sync_api():
    """MCPBridge 同步 API 正常工作"""
    from dot.mcp import MCPBridge

    bridge = MCPBridge()
    try:
        servers = bridge.list_servers()
        assert isinstance(servers, list)
        tools = bridge.list_tools()
        assert isinstance(tools, list)
    finally:
        bridge.disconnect_all()


# 6. 模块导出不包含旧类型
def test_no_legacy_exports():
    """__init__.py 不再导出自研类型"""
    import dot.mcp as mcp

    assert not hasattr(mcp, "MCPClient"), "旧 MCPClient 应从导出中移除"
    assert not hasattr(mcp, "MCPToolResult"), "自研 MCPToolResult 应移除"
