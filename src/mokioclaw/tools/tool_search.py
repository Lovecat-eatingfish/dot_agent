"""
ToolSearch —— 延迟工具 schema 按需加载（对齐 Claude Code ToolSearchTool）

策略：
- alwaysLoad：核心工具始终 bind（读写 / Grep / Bash / ToolSearch 本身）
- deferred：重型 / 可选工具（WebSearch、Agent、Skill、MemoryWrite、大量 MCP）
  仅在目录中出现名称+简述；模型调用 ToolSearch 后再 bind 完整 schema
"""
from __future__ import annotations

import os
from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 永不延迟的核心工具
ALWAYS_LOAD = frozenset({
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GlobTool",
    "GrepTool",
    "BashTool",
    "TodoUpdateTool",
    "ToolSearchTool",
    "LoadMcpTool",
    "MemoryIndexTool",
    "MemoryReadTool",
})

# 可延迟工具（内置侧）
DEFERRED_BUILTIN = frozenset({
    "WebSearchTool",
    "SkillTool",
    "AgentTool",
    "BackgroundTaskStatus",
    "BackgroundTaskCancel",
    "MemoryWriteTool",
})


def tool_search_enabled() -> bool:
    return os.getenv("MOKIO_TOOL_SEARCH", "1").strip().lower() not in {"0", "false", "no", "off"}


def is_deferred_tool(name: str, *, always_load: frozenset[str] | None = None) -> bool:
    always = always_load or ALWAYS_LOAD
    if name in always:
        return False
    if name in DEFERRED_BUILTIN:
        return True
    if name.startswith("mcp__"):
        return True
    return False


def partition_tools(
    tools: list[StructuredTool],
    loaded: dict[str, StructuredTool] | None = None,
) -> tuple[list[StructuredTool], list[StructuredTool]]:
    """拆成 (bind 列表, deferred 目录项)"""
    loaded = loaded or {}
    if not tool_search_enabled():
        return list(tools), []

    bind: list[StructuredTool] = []
    deferred: list[StructuredTool] = []
    for tool in tools:
        if tool.name in loaded or not is_deferred_tool(tool.name):
            bind.append(tool)
        else:
            deferred.append(tool)
    return bind, deferred


def build_deferred_catalog(deferred: list[StructuredTool]) -> str:
    if not deferred:
        return ""
    lines = ["Deferred tools (call ToolSearchTool to load full schema before use):"]
    for tool in deferred[:40]:
        desc = (tool.description or "").split("\n")[0][:100]
        lines.append(f"- {tool.name}: {desc}")
    if len(deferred) > 40:
        lines.append(f"... and {len(deferred) - 40} more")
    return "\n".join(lines)


def build_tool_search_tool(
    deferred_pool: dict[str, StructuredTool],
    loaded: dict[str, StructuredTool],
) -> StructuredTool:
    """query: 关键词或精确工具名；select: select:ToolName 直接加载"""

    def _search(query: str) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {
                "ok": False,
                "error": "query required",
                "hint": "Use 'select:ToolName' or a keyword like 'web' / 'agent'",
            }

        # 精确加载
        if q.lower().startswith("select:"):
            name = q.split(":", 1)[1].strip()
            tool = deferred_pool.get(name) or loaded.get(name)
            if tool is None:
                return {
                    "ok": False,
                    "error": f"unknown tool: {name}",
                    "available": sorted(deferred_pool)[:30],
                }
            loaded[name] = tool
            return {
                "ok": True,
                "loaded": [name],
                "description": tool.description,
                "hint": "Schema loaded; call this tool in a following round.",
            }

        # 关键词搜索
        needle = q.lower()
        matches = [
            t
            for name, t in deferred_pool.items()
            if needle in name.lower() or needle in (t.description or "").lower()
        ]
        # 也搜已加载的（便于确认）
        matches.extend(
            t
            for name, t in loaded.items()
            if needle in name.lower() and t not in matches
        )
        if not matches:
            return {
                "ok": False,
                "error": f"no deferred tools match '{q}'",
                "available": sorted(deferred_pool)[:30],
            }

        loaded_names = []
        for tool in matches[:8]:
            loaded[tool.name] = tool
            loaded_names.append(tool.name)
        return {
            "ok": True,
            "loaded": loaded_names,
            "matches": [
                {"name": t.name, "description": (t.description or "")[:160]}
                for t in matches[:8]
            ],
            "hint": "Schemas loaded; call them in a following tool round.",
        }

    return StructuredTool.from_function(
        name="ToolSearchTool",
        func=_search,
        description=(
            "Search and load deferred tool schemas by keyword or 'select:ToolName'. "
            "Required before calling WebSearchTool, AgentTool, SkillTool, MemoryWriteTool, "
            "or deferred MCP tools when Tool Search is enabled."
        ),
    )


def apply_tool_search_filter(
    tools: list[StructuredTool],
    runtime: Any,
) -> list[StructuredTool]:
    """对完整工具列表应用延迟加载过滤，并确保 ToolSearchTool 在 bind 集中"""
    if not tool_search_enabled():
        return tools

    # 注意：空 dict 在布尔上下文为 False，不能用 `or {}`，否则会丢掉 runtime 上的同一引用
    loaded = getattr(runtime, "loaded_tools", None)
    if not isinstance(loaded, dict):
        loaded = {}
        runtime.loaded_tools = loaded

    # MCP 已加载的也并入
    mcp_loaded = getattr(runtime, "loaded_mcp_tools", None)
    if isinstance(mcp_loaded, dict):
        for name, tool in mcp_loaded.items():
            loaded.setdefault(name, tool)

    bind, deferred = partition_tools(tools, loaded)
    deferred_pool = {t.name: t for t in deferred}
    # 缓存目录供 prompt 使用
    runtime.deferred_tool_catalog = build_deferred_catalog(deferred)

    if deferred_pool:
        # 避免重复 ToolSearchTool
        bind = [t for t in bind if t.name != "ToolSearchTool"]
        bind.append(build_tool_search_tool(deferred_pool, loaded))
        TOOL_CONCURRENCY_META_LOCAL = True
        from mokioclaw.tools.registry import TOOL_CONCURRENCY_META

        TOOL_CONCURRENCY_META["ToolSearchTool"] = TOOL_CONCURRENCY_META_LOCAL

    return bind
