"""
dot.coding.tools.glob_tool — 文件搜索工具

基于 glob 模式的文件搜索，注册为 AgentTool frozen dataclass。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dot.ai.types import TextContent
from dot.agent.tools import AgentTool, AgentToolResult, JSONValue


async def _glob_search(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    pattern = str(arguments.get("pattern", "**/*"))
    search_path = str(arguments.get("path", "."))
    head_limit = int(arguments.get("head_limit", 200))

    base = (workspace / search_path).resolve() if not Path(search_path).is_absolute() else Path(search_path)
    if not str(base).startswith(str(workspace.resolve())):
        return AgentToolResult(content=[TextContent(text="Path must be inside workspace")])

    try:
        matches = sorted(base.glob(pattern))[:head_limit]
        relative = []
        for p in matches:
            try:
                relative.append(str(p.resolve().relative_to(workspace.resolve())).replace("\\", "/"))
            except ValueError:
                relative.append(str(p))

        if not relative:
            return AgentToolResult(content=[TextContent(text="No files found matching the pattern")])

        text = "\n".join(relative)
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={"count": len(relative), "pattern": pattern},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Glob error: {exc}")])


def create_glob_tool(state: dict) -> AgentTool:
    return AgentTool(
        name="glob_search",
        label="Glob Search",
        description="Find files or directories by glob pattern (e.g. '**/*.py', '*.md').",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search root (relative to workspace)", "default": "."},
                "head_limit": {"type": "integer", "description": "Max results", "default": 200},
            },
            "required": ["pattern"],
        },
        execute_fn=_glob_search,
    )
