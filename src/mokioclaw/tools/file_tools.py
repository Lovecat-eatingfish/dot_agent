from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from mokioclaw.core.state import RuntimeState

MAX_READ_LINES = 2000
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


def _strip_workspace_prefix(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").strip()
    while normalized in {"workspace", "./workspace"} or normalized.startswith(("workspace/", "./workspace/")):
        if normalized in {"workspace", "./workspace"}:
            normalized = "."
        elif normalized.startswith("./workspace/"):
            normalized = normalized[len("./workspace/") :]
        else:
            normalized = normalized[len("workspace/") :]
    return normalized


def read_text_lossy(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def resolve_workspace_path(state: RuntimeState, file_path: str) -> Path:
    raw = Path(_strip_workspace_prefix(file_path)).expanduser()
    if not raw.is_absolute():
        raw = state.workspace / raw
    return state.assert_workspace_path(raw)


def display_path(state: RuntimeState, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(state.workspace.resolve()))
    except ValueError:
        return str(path)


def read_file(
    state: RuntimeState,
    file_path: str,
    offset: int | str = 0,
    limit: int | str = MAX_READ_LINES,
) -> dict[str, Any]:
    try:
        path = resolve_workspace_path(state, file_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not path.exists():
        return {"ok": False, "error": f"file does not exist: {display_path(state, path)}"}
    if not path.is_file():
        return {"ok": False, "error": f"path is not a file: {display_path(state, path)}"}
    try:
        offset_value = int(offset)
        limit_value = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "offset and limit must be integers"}
    if offset_value < 0:
        return {"ok": False, "error": "offset must be >= 0"}
    if limit_value <= 0:
        return {"ok": False, "error": "limit must be > 0"}

    text = read_text_lossy(path)
    lines = text.splitlines()
    limit_value = min(limit_value, MAX_READ_LINES)
    selected = lines[offset_value : offset_value + limit_value]
    complete = offset_value == 0 and len(selected) == len(lines)
    state.record_read(path, complete=complete)

    numbered = "\n".join(f"{offset_value + idx + 1}: {line}" for idx, line in enumerate(selected))
    return {
        "ok": True,
        "path": display_path(state, path),
        "total_lines": len(lines),
        "offset": offset_value,
        "limit": limit_value,
        "complete": complete,
        "content": numbered,
    }


def write_file(state: RuntimeState, file_path: str, content: str) -> dict[str, Any]:
    try:
        path = resolve_workspace_path(state, file_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    existed = path.exists()

    if existed:
        snapshot = state.snapshot_for(path)
        if snapshot is None:
            return {"ok": False, "error": "file has not been read yet. Read it before overwriting."}
        if path.stat().st_mtime_ns != snapshot.mtime_ns:
            return {"ok": False, "error": "file changed after it was read. Read it again before writing."}
        original = read_text_lossy(path)
    else:
        original = ""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    state.record_read(path, complete=True)

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            content.splitlines(),
            fromfile=f"a/{display_path(state, path)}",
            tofile=f"b/{display_path(state, path)}",
            lineterm="",
        )
    )
    return {
        "ok": True,
        "type": "update" if existed else "create",
        "path": display_path(state, path),
        "lines": len(content.splitlines()),
        "diff": diff[:4000],
    }


def edit_file(state: RuntimeState, file_path: str, old_text: str, new_text: str) -> dict[str, Any]:
    try:
        path = resolve_workspace_path(state, file_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not path.exists():
        return {"ok": False, "error": f"file does not exist: {display_path(state, path)}"}

    snapshot = state.snapshot_for(path)
    if snapshot is None:
        return {"ok": False, "error": "file has not been read yet. Read it before editing."}
    if path.stat().st_mtime_ns != snapshot.mtime_ns:
        return {"ok": False, "error": "file changed after it was read. Read it again before editing."}
    if not old_text:
        return {"ok": False, "error": "old_text must not be empty"}

    original = read_text_lossy(path)
    count = original.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old_text was not found"}
    if count > 1:
        return {"ok": False, "error": f"old_text matched {count} times. Provide a unique snippet."}

    updated = original.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    state.record_read(path, complete=True)

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{display_path(state, path)}",
            tofile=f"b/{display_path(state, path)}",
            lineterm="",
        )
    )
    return {
        "ok": True,
        "path": display_path(state, path),
        "replacements": 1,
        "diff": diff[:4000],
    }
