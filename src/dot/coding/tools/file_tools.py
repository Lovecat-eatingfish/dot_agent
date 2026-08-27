"""
dot.coding.tools.file_tools — 文件操作工具

read_file / write_file / edit_file，注册为 AgentTool frozen dataclass。
移除 langchain StructuredTool 依赖，使用新的工具系统。
"""
from __future__ import annotations

import difflib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dot.ai.types import TextContent
from dot.agent.tools import AgentTool, AgentToolResult, JSONValue

TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")
MAX_READ_LINES = 2000


def decode_text_lossy(data: bytes) -> str:
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_text_lossy(path: Path) -> str:
    return decode_text_lossy(path.read_bytes())


def _strip_workspace_prefix(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").strip()
    while normalized in {"workspace", "./workspace"} or normalized.startswith(("workspace/", "./workspace/")):
        if normalized in {"workspace", "./workspace"}:
            normalized = "."
        elif normalized.startswith("./workspace/"):
            normalized = normalized[len("./workspace/"):]
        else:
            normalized = normalized[len("workspace/"):]
    return normalized


def _resolve_path(workspace: Path, file_path: str) -> Path:
    raw = Path(_strip_workspace_prefix(file_path)).expanduser()
    if not raw.is_absolute():
        raw = workspace / raw
    return raw.resolve()


def _display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


# ============================================================
# read_file
# ============================================================

async def _read_file(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    file_path = str(arguments.get("file_path", ""))
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", MAX_READ_LINES))

    try:
        path = _resolve_path(workspace, file_path)
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Path error: {exc}")])

    if not path.exists():
        return AgentToolResult(content=[TextContent(text=f"File not found: {_display_path(workspace, path)}")])
    if not path.is_file():
        return AgentToolResult(content=[TextContent(text=f"Not a file: {_display_path(workspace, path)}")])

    try:
        raw = path.read_bytes()
        text = decode_text_lossy(raw)
        lines = text.splitlines()
        limit = min(limit, MAX_READ_LINES)
        selected = lines[offset:offset + limit]
        complete = offset == 0 and len(selected) == len(lines)
        numbered = "\n".join(f"{offset + i + 1}: {line}" for i, line in enumerate(selected))
        return AgentToolResult(
            content=[TextContent(text=numbered)],
            details={"path": _display_path(workspace, path), "total_lines": len(lines), "complete": complete},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Read error: {exc}")])


def create_read_tool(state: dict) -> AgentTool:
    return AgentTool(
        name="read_file",
        label="Read File",
        description="Read a UTF-8 text file. Supports offset and limit for pagination.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "offset": {"type": "integer", "description": "Start line (0-based)", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
            },
            "required": ["file_path"],
        },
        execute_fn=_read_file,
    )


# ============================================================
# write_file
# ============================================================

async def _write_file(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    file_path = str(arguments.get("file_path", ""))
    content = str(arguments.get("content", ""))

    try:
        path = _resolve_path(workspace, file_path)
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Path error: {exc}")])

    existed = path.exists()
    original = ""
    if existed:
        try:
            original = read_text_lossy(path)
        except Exception:
            pass

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), content.splitlines(),
            fromfile=f"a/{_display_path(workspace, path)}",
            tofile=f"b/{_display_path(workspace, path)}",
            lineterm="",
        ))
        return AgentToolResult(
            content=[TextContent(text=f"{'Updated' if existed else 'Created'}: {_display_path(workspace, path)}")],
            details={"type": "update" if existed else "create", "lines": len(content.splitlines()), "diff": diff[:4000]},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Write error: {exc}")])


def create_write_tool(state: dict) -> AgentTool:
    return AgentTool(
        name="write_file",
        label="Write File",
        description="Create or rewrite a file inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["file_path", "content"],
        },
        execute_fn=_write_file,
    )


# ============================================================
# edit_file
# ============================================================

async def _edit_file(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    file_path = str(arguments.get("file_path", ""))
    old_text = str(arguments.get("old_text", ""))
    new_text = str(arguments.get("new_text", ""))

    try:
        path = _resolve_path(workspace, file_path)
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Path error: {exc}")])

    if not path.exists():
        return AgentToolResult(content=[TextContent(text=f"File not found: {_display_path(workspace, path)}")])

    try:
        original = read_text_lossy(path)
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Read error: {exc}")])

    count = original.count(old_text)
    if count == 0 and "\r\n" in original:
        original = original.replace("\r\n", "\n")
        count = original.count(old_text)
    if count == 0:
        return AgentToolResult(content=[TextContent(text="old_text was not found in the file")])
    if count > 1:
        return AgentToolResult(content=[TextContent(text=f"old_text matched {count} times. Provide a unique snippet.")])

    updated = original.replace(old_text, new_text, 1)
    try:
        path.write_text(updated, encoding="utf-8", newline="\n")
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), updated.splitlines(),
            fromfile=f"a/{_display_path(workspace, path)}",
            tofile=f"b/{_display_path(workspace, path)}",
            lineterm="",
        ))
        return AgentToolResult(
            content=[TextContent(text=f"Edited: {_display_path(workspace, path)}")],
            details={"replacements": 1, "diff": diff[:4000]},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Write error: {exc}")])


def create_edit_tool(state: dict) -> AgentTool:
    return AgentTool(
        name="edit_file",
        label="Edit File",
        description="Edit a file by replacing one unique old_text snippet with new_text.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "old_text": {"type": "string", "description": "Text to find and replace"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["file_path", "old_text", "new_text"],
        },
        execute_fn=_edit_file,
    )
