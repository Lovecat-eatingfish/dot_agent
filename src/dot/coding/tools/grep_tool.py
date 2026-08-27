"""
dot.coding.tools.grep_tool — 内容搜索工具

基于 ripgrep 或纯 Python 的文本搜索，注册为 AgentTool frozen dataclass。
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from dot.ai.types import TextContent
from dot.agent.tools import AgentTool, AgentToolResult, JSONValue

MAX_RESULTS = 100


async def _grep(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    pattern = str(arguments.get("pattern", ""))
    search_path = str(arguments.get("path", "."))
    glob_filter = str(arguments.get("glob", "")) or None
    head_limit = int(arguments.get("head_limit", MAX_RESULTS))
    ignore_case = bool(arguments.get("ignore_case", False))

    if not pattern:
        return AgentToolResult(content=[TextContent(text="Pattern must not be empty")])

    base = (workspace / search_path).resolve() if not Path(search_path).is_absolute() else Path(search_path)

    # Try ripgrep first
    try:
        cmd = ["rg", "--no-heading", "-n", pattern, str(base)]
        if ignore_case:
            cmd.insert(1, "-i")
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            lines = proc.stdout.strip().split("\n")[:head_limit]
            return AgentToolResult(
                content=[TextContent(text="\n".join(lines))],
                details={"count": len(lines), "tool": "ripgrep"},
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: pure Python
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        results = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if glob_filter and not path.match(glob_filter):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        try:
                            rel = str(path.resolve().relative_to(workspace.resolve())).replace("\\", "/")
                        except ValueError:
                            rel = str(path)
                        results.append(f"{rel}:{i}: {line}")
                        if len(results) >= head_limit:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(results) >= head_limit:
                break

        if not results:
            return AgentToolResult(content=[TextContent(text="No matches found")])
        return AgentToolResult(
            content=[TextContent(text="\n".join(results))],
            details={"count": len(results), "tool": "python"},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Grep error: {exc}")])


def create_grep_tool(state: dict) -> AgentTool:
    return AgentTool(
        name="grep",
        label="Grep",
        description="Search workspace text files by regex pattern. Uses ripgrep when available.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search"},
                "path": {"type": "string", "description": "Search root (relative to workspace)", "default": "."},
                "glob": {"type": "string", "description": "File filter (e.g. '*.py')"},
                "head_limit": {"type": "integer", "description": "Max results", "default": 100},
                "ignore_case": {"type": "boolean", "description": "Case insensitive", "default": False},
            },
            "required": ["pattern"],
        },
        execute_fn=_grep,
    )
