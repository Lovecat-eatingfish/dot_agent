from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools.bash_tool import bash_tool_description, run_bash, run_bash_read_only
from mokioclaw.tools.file_tools import edit_file, read_file, write_file
from mokioclaw.tools.glob_tool import glob_search
from mokioclaw.tools.grep_tool import grep
from mokioclaw.tools.notepad_tool import append_notepad, read_notepad
from mokioclaw.tools.skill import Skill, discover_skills, load_skill_markdown
from mokioclaw.tools.web_search_tool import build_web_search_tool

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 工具并发安全元数据
# is_concurrency_safe=True 的工具可以安全并行执行（只读、无副作用）
# is_concurrency_safe=False 的工具会修改磁盘/状态，必须串行执行
# ---------------------------------------------------------------------------
TOOL_CONCURRENCY_META: dict[str, bool] = {
    "FileReadTool": True,
    "GlobTool": True,
    "GrepTool": True,
    "NotepadReadTool": True,
    "WebSearchTool": True,
    "SkillTool": True,
    "ToolSearchTool": True,
    "LoadMcpTool": True,
    "FileWriteTool": False,
    "FileEditTool": False,
    "BashTool": False,
    "NotepadAppendTool": False,
    "TodoUpdateTool": False,
}


def is_tool_concurrency_safe(name: str) -> bool:
    """判断工具是否可并行

    MCP 工具默认按写操作处理（unsafe），除非显式登记。
    """
    if name in TOOL_CONCURRENCY_META:
        return TOOL_CONCURRENCY_META[name]
    if name.startswith("mcp__"):
        return TOOL_CONCURRENCY_META.get(name, False)
    return False


def build_tools(state: RuntimeState, *, include_mcp: bool = True, include_skills: bool = True) -> list[StructuredTool]:
    tools = [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace. Supports offset and limit.",
        ),
        StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: write_file(state, file_path, content),
            description="Create a new file or rewrite an existing file inside the workspace.",
        ),
        StructuredTool.from_function(
            name="FileEditTool",
            func=lambda file_path, old_text, new_text: edit_file(state, file_path, old_text, new_text),
            description="Edit an existing workspace file by replacing one unique old_text snippet.",
        ),
        StructuredTool.from_function(
            name="GlobTool",
            func=lambda pattern, path=".", path_type="file", head_limit=200: glob_search(
                state, pattern, path, path_type, head_limit
            ),
            description=(
                "Find files or directories inside the workspace by glob pattern. "
                "Args: pattern (e.g. '**/*.py', '*.md'), optional path, optional path_type "
                "('file' or 'dir'), optional head_limit. Returns sorted relative paths."
            ),
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False,
            context_before=0, context_after=0, output_mode="content": grep(
                state, pattern, path, glob, head_limit, ignore_case,
                context_before, context_after, output_mode,
            ),
            description=(
                "Search workspace text files by regex pattern. Uses ripgrep when available, "
                "falls back to a pure-Python scanner. Args: pattern, optional path, optional glob "
                "filter, head_limit, ignore_case, context_before, context_after, output_mode "
                "('content' | 'files_with_matches' | 'count')."
            ),
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        StructuredTool.from_function(
            name="NotepadAppendTool",
            func=lambda heading, content: append_notepad(state, heading, content),
            description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
        ),
        build_web_search_tool(workspace=state.workspace),
    ]

    if include_skills:
        skill_tool = _build_skill_tool(state)
        if skill_tool is not None:
            tools.append(skill_tool)

    try:
        from mokioclaw.tools.memory_tools import build_memory_tools
        tools.extend(build_memory_tools(state))
    except Exception as exc:
        logger.debug("Memory tools not loaded: %s", exc)

    # Agent / BackgroundTask*：到达嵌套深度上限后不再下发（对齐 Claude Code 深度限制）
    try:
        from mokioclaw.tools.agent_tool import build_agent_tools, max_subagent_depth

        depth = int(getattr(state, "_subagent_depth", 0))
        if depth < max_subagent_depth():
            tools.extend(build_agent_tools(state))
    except Exception as exc:
        logger.debug("Agent tools not loaded: %s", exc)

    if include_mcp:
        tools.extend(_load_mcp_tools(state))

    # ToolSearch 延迟加载（对齐 Claude Code ToolSearchTool）
    try:
        from mokioclaw.tools.tool_search import apply_tool_search_filter

        tools = apply_tool_search_filter(tools, state)
    except Exception as exc:
        logger.debug("tool search filter skipped: %s", exc)

    return tools


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace. Supports offset and limit.",
        ),
        StructuredTool.from_function(
            name="GlobTool",
            func=lambda pattern, path=".", path_type="file", head_limit=200: glob_search(
                state, pattern, path, path_type, head_limit
            ),
            description=(
                "Find files or directories inside the workspace by glob pattern. "
                "Args: pattern (e.g. '**/*.py', '*.md'), optional path, optional path_type "
                "('file' or 'dir'), optional head_limit. Returns sorted relative paths."
            ),
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False,
            context_before=0, context_after=0, output_mode="content": grep(
                state, pattern, path, glob, head_limit, ignore_case,
                context_before, context_after, output_mode,
            ),
            description=(
                "Search workspace text files by regex pattern. Uses ripgrep when available, "
                "falls back to a pure-Python scanner. Args: pattern, optional path, optional glob "
                "filter, head_limit, ignore_case, context_before, context_after, output_mode "
                "('content' | 'files_with_matches' | 'count')."
            ),
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash_read_only(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        build_web_search_tool(workspace=state.workspace),
    ]


def _build_skill_tool(state: RuntimeState) -> StructuredTool | None:
    skills = _discover_runtime_skills(state)
    if not skills:
        return None
    catalog = {s.name: s for s in skills}

    def _invoke(skill_name: str) -> dict[str, Any]:
        skill = catalog.get(skill_name)
        if skill is None:
            # 大小写不敏感
            for key, value in catalog.items():
                if key.lower() == skill_name.lower():
                    skill = value
                    break
        if skill is None:
            available = ", ".join(sorted(catalog))
            return {"ok": False, "error": f"unknown skill: {skill_name}", "available": available}
        body = load_skill_markdown(skill)
        return {
            "ok": True,
            "skill": skill.name,
            "description": skill.description,
            "content": body,
            "hint": "Follow the skill instructions in subsequent tool calls.",
        }

    names = ", ".join(sorted(catalog))
    return StructuredTool.from_function(
        name="SkillTool",
        func=_invoke,
        description=(
            "Load a skill SOP by name and return its full instructions. "
            f"Available skills: {names}. Prefer this when a task matches a skill description."
        ),
    )


def _discover_runtime_skills(state: RuntimeState) -> list[Skill]:
    skills: list[Skill] = []
    seen: set[str] = set()
    for directory in (
        Path.home() / ".mokioclaw" / "skills",
        state.workspace / ".mokioclaw" / "skills",
    ):
        for skill in discover_skills(directory):
            if skill.name not in seen:
                skills.append(skill)
                seen.add(skill.name)
    try:
        from mokioclaw.plugins.loader import discover_plugin_skills

        for skill in discover_plugin_skills(state.workspace):
            if skill.name not in seen:
                skills.append(skill)
                seen.add(skill.name)
    except Exception as exc:
        logger.debug("plugin skills not loaded: %s", exc)
    return skills


def _load_mcp_tools(state: RuntimeState) -> list[StructuredTool]:
    try:
        from mokioclaw.mcp.bridge import get_mcp_bridge
        from mokioclaw.mcp.disclosure import select_mcp_tools_for_bind

        bridge = get_mcp_bridge(state.workspace)
        tools = select_mcp_tools_for_bind(bridge, getattr(state, "loaded_mcp_tools", {}))
        for tool in tools:
            # LoadMcpTool 只读安全；其余 MCP 默认 unsafe
            if tool.name == "LoadMcpTool":
                TOOL_CONCURRENCY_META.setdefault(tool.name, True)
            else:
                TOOL_CONCURRENCY_META.setdefault(tool.name, False)
        return tools
    except Exception as exc:
        logger.debug("MCP tools not loaded: %s", exc)
        return []
