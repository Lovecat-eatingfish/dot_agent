"""
MCP Tool Search / 渐进披露

对齐 Claude Code：
- system 动态区只注入 MCP 服务器「目录」（简短描述）
- 具体 schema 通过 LoadMcpTool 按需加载后再 bind

阈值：MCP 工具数 > MCP_DISCLOSURE_THRESHOLD 时启用延迟加载。
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

MCP_DISCLOSURE_THRESHOLD = 12


def build_mcp_catalog_text(bridge: Any) -> str:
    """生成注入 system 动态区的 MCP 目录

    对齐 Claude Code：目录同时列出 tools 与 resources，供模型感知可用资源。
    """
    try:
        servers = bridge.list_servers()
    except Exception:
        return ""
    if not servers:
        return ""
    lines = ["MCP servers available (use LoadMcpTool to load schemas when needed):"]
    for name in servers:
        tools = bridge.list_tools(name)
        brief = ", ".join(t.name for t in tools[:8])
        more = f" (+{len(tools) - 8} more)" if len(tools) > 8 else ""
        lines.append(f"- {name}: {len(tools)} tools — {brief}{more}")
        # 资源目录（对齐 Claude Code resources 注入）
        try:
            resources = bridge.list_resources(name) if hasattr(bridge, "list_resources") else []
        except Exception:
            resources = []
        if resources:
            res_brief = ", ".join(r.name or r.uri for r in resources[:8])
            res_more = f" (+{len(resources) - 8} more)" if len(resources) > 8 else ""
            lines.append(f"  resources: {res_brief}{res_more}")
    return "\n".join(lines)


def should_defer_mcp_schemas(bridge: Any) -> bool:
    try:
        return len(bridge.list_tools()) > MCP_DISCLOSURE_THRESHOLD
    except Exception:
        return False


def build_load_mcp_tool(bridge: Any, loaded: dict[str, StructuredTool]) -> StructuredTool:
    """按需加载某个 MCP 工具完整 schema 到会话工具集"""

    def _load(tool_name: str) -> dict[str, Any]:
        """tool_name: mcp__server__tool 或 server:tool"""
        tools = {t.name: t for t in bridge.to_langchain_tools()}
        # 兼容简写 server__tool
        match = tools.get(tool_name)
        if match is None:
            for name, tool in tools.items():
                if name.endswith(f"__{tool_name}") or name.endswith(f":{tool_name}"):
                    match = tool
                    tool_name = name
                    break
        if match is None:
            available = sorted(tools)[:30]
            return {"ok": False, "error": f"unknown MCP tool: {tool_name}", "available": available}
        loaded[tool_name] = match
        return {
            "ok": True,
            "loaded": tool_name,
            "description": match.description,
            "hint": (
                "Tool schema loaded. You may call it in a following tool round "
                "(same agent turn), or later in this session."
            ),
        }

    return StructuredTool.from_function(
        name="LoadMcpTool",
        func=_load,
        description=(
            "Load a deferred MCP tool schema by full name (mcp__server__tool). "
            "Use after consulting the MCP catalog in the system prompt."
        ),
    )


def select_mcp_tools_for_bind(bridge: Any, loaded: dict[str, StructuredTool] | None = None) -> list[StructuredTool]:
    """决定本轮 bind 哪些 MCP 工具"""
    loaded = loaded if loaded is not None else {}
    if not should_defer_mcp_schemas(bridge):
        return bridge.to_langchain_tools()
    # 延迟模式：只暴露 LoadMcpTool + 已加载的工具
    tools = [build_load_mcp_tool(bridge, loaded)]
    tools.extend(loaded.values())
    return tools
