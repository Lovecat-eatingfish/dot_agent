# mcp 接入问题
1. 不需要 自己实现连接， 用mcp提供的客户端就好了, 示例如下
```python

import asyncio
from contextlib import AsyncExitStack
from typing import Optional, List
from mcp import ClientSession
from mcp.client.sse import sse_client
import mcp.types as types


class RemoteMCPClient:
    def __init__(self):
        self._stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None

    async def connect_sse(self, url: str, headers: dict = None):
        """连接远程SSE MCP Server（百度等云端MCP）"""
        streams = await self._stack.enter_async_context(
            sse_client(url, headers=headers or {}, timeout=60)
        )
        self.session = await self._stack.enter_async_context(ClientSession(*streams))
        # MCP初始化握手
        await self.session.initialize()

    async def list_tools(self) -> List[types.Tool]:
        resp = await self.session.list_tools()
        return resp.tools

    async def call_tool(self, tool_name: str, arguments: dict):
        return await self.session.call_tool(tool_name, arguments=arguments)

    async def close(self):
        await self._stack.aclose()


async def demo():
    client = RemoteMCPClient()
    # 接入百度远程MCP
    await client.connect_sse(url="https://mcp.map.baidu.com/sse?ak=310ec3ecf465b022c2a1a2e82f29318d")
    tools = await client.list_tools()
    print(tools)
    await client.close()

if __name__ == "__main__":
    asyncio.run(demo())

```
