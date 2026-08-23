import asyncio
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def run():
    url = "https://mcpmarket.cn/mcp/6cacb65d6dd0ea402c3a8a39/sse"
    async with sse_client(url) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool("天气查询", arguments={"city":"杭州"})
            print(result.content)


if __name__ == "__main__":
    asyncio.run(run())
