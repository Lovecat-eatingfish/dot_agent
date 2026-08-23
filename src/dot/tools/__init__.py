"""
dot 工具构建入口（registry）

构建 coding/valid 节点使用的基础工具集：
FileReadTool / FileWriteTool / FileEditTool / GlobTool / GrepTool / BashTool

MCP / Skills 工具不在此处构建 —— 由 session 的 mcp_host / skill_host
通过渐进披露元工具（mcp_search / skills_search）按需加载。
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from .bash_tool import bash_tool_description, run_bash, run_bash_read_only
from .file_tools import edit_file, read_file, write_file
from .glob_tool import glob_search
from .grep_tool import grep

__all__ = [
    "build_tools",
    "build_read_only_tools",
    "is_tool_concurrency_safe",
    "TOOL_CONCURRENCY_META",
]


# ---------------------------------------------------------------------------
# 工具并发安全元数据
# is_concurrency_safe=True 的工具可以安全并行执行（只读、无副作用）
# ---------------------------------------------------------------------------

TOOL_CONCURRENCY_META: dict[str, bool] = {
    "FileReadTool": True,
    "GlobTool": True,
    "GrepTool": True,
    "LoadMcpTool": True,
    "FileWriteTool": False,
    "FileEditTool": False,
    "BashTool": False,
}


def is_tool_concurrency_safe(name: str) -> bool:
    """判断工具是否可并行（MCP 工具默认按写操作处理）"""
    if name in TOOL_CONCURRENCY_META:
        return TOOL_CONCURRENCY_META[name]
    if name.startswith("mcp__"):
        return TOOL_CONCURRENCY_META.get(name, False)
    return False


def build_tools(state: Any) -> list[StructuredTool]:
    """构建完整基础工具集（读 + 写）"""
    return _build_base_tools(state, read_only=False)


def build_read_only_tools(state: Any) -> list[StructuredTool]:
    """构建只读工具集（verifier 专用）"""
    return _build_base_tools(state, read_only=True)


def _build_base_tools(state: Any, *, read_only: bool) -> list[StructuredTool]:
    def _bash(command, timeout_seconds=None, run_in_background=False):
        if read_only:
            return run_bash_read_only(state, command, timeout_seconds, run_in_background)
        return run_bash(state, command, timeout_seconds, run_in_background)

    tools: list[StructuredTool] = [
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
            func=_bash,
            description=bash_tool_description(),
        ),
    ]
    if not read_only:
        tools.insert(1, StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: write_file(state, file_path, content),
            description="Create a new file or rewrite an existing file inside the workspace.",
        ))
        tools.insert(2, StructuredTool.from_function(
            name="FileEditTool",
            func=lambda file_path, old_text, new_text: edit_file(state, file_path, old_text, new_text),
            description="Edit an existing workspace file by replacing one unique old_text snippet.",
        ))
    return tools
