"""
dot.coding.review.previewer — 写操作执行前的 diff 预览

在权限 ASK 审批（ask_user）阶段调用：不落盘，纯模拟。
- write_file : 现有内容（若有）vs 新 content 的 unified diff
- edit_file  : 应用 old_text → new_text 模拟替换后的 unified diff，
               并提前暴露 old_text 匹配 0 次 / 多次的错误（省一次无效执行）

模拟逻辑必须与 dot.coding.tools.file_tools 的执行逻辑一致（含 CRLF 兜底），
避免"预览说不行、执行却可以"的偏差。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from dot.agent.tools import JSONValue
from dot.coding.tools.file_tools import decode_text_lossy

from .fsutil import (
    diff_stat,
    display_path,
    is_probably_binary,
    resolve,
    truncate_diff,
    unified_diff,
)

# ChangePreview.kind 取值
KIND_CREATE = "create"
KIND_UPDATE = "update"
KIND_BINARY = "binary"
KIND_ERROR = "error"


@dataclass(frozen=True)
class ChangePreview:
    """一次写操作的执行前预览（ask_user info["preview"] 的载体）"""
    kind: str = KIND_ERROR          # create | update | binary | error
    path: str = ""                  # workspace 相对展示路径
    diff: str = ""                  # 已截断的 unified diff
    truncated: bool = False
    error: str = ""                 # kind=error/binary 时的说明
    lines_added: int = 0
    lines_removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_preview(
    tool_name: str,
    args: Mapping[str, JSONValue],
    workspace: Path,
) -> ChangePreview:
    """为写工具构建执行前预览；任何异常都折价为 kind=error，绝不抛出"""
    try:
        if tool_name == "write_file":
            return _preview_write(args, workspace)
        if tool_name == "edit_file":
            return _preview_edit(args, workspace)
    except Exception as exc:
        return ChangePreview(kind=KIND_ERROR, error=str(exc))
    return ChangePreview(kind=KIND_ERROR, error=f"no preview for tool: {tool_name}")


def _finished(kind: str, display: str, before: str, after: str, error: str = "") -> ChangePreview:
    diff = unified_diff(before, after, display) if not error else ""
    added, removed = diff_stat(diff)
    text, truncated = truncate_diff(diff)
    return ChangePreview(
        kind=kind, path=display, diff=text, truncated=truncated,
        error=error, lines_added=added, lines_removed=removed,
    )


def _preview_write(args: Mapping[str, Any], workspace: Path) -> ChangePreview:
    file_path = str(args.get("file_path", ""))
    content = str(args.get("content", ""))
    path = resolve(workspace, file_path)
    display = display_path(workspace, path)

    if path.exists():
        raw = path.read_bytes()
        if is_probably_binary(raw):
            return ChangePreview(kind=KIND_BINARY, path=display,
                                 error=f"binary file ({len(raw)} bytes)")
        before = decode_text_lossy(raw)
        return _finished(KIND_UPDATE, display, before, content)
    return _finished(KIND_CREATE, display, "", content)


def _preview_edit(args: Mapping[str, Any], workspace: Path) -> ChangePreview:
    file_path = str(args.get("file_path", ""))
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    path = resolve(workspace, file_path)
    display = display_path(workspace, path)

    if not path.exists():
        return ChangePreview(kind=KIND_ERROR, path=display, error="file not found")
    raw = path.read_bytes()
    if is_probably_binary(raw):
        return ChangePreview(kind=KIND_BINARY, path=display,
                             error=f"binary file ({len(raw)} bytes)")

    original = decode_text_lossy(raw)
    count = original.count(old_text)
    if count == 0 and "\r\n" in original:
        original = original.replace("\r\n", "\n")
        count = original.count(old_text)
    if count == 0:
        return ChangePreview(kind=KIND_ERROR, path=display,
                             error="old_text was not found in the file")
    if count > 1:
        return ChangePreview(kind=KIND_ERROR, path=display,
                             error=f"old_text matched {count} times; provide a unique snippet")
    updated = original.replace(old_text, new_text, 1)
    return _finished(KIND_UPDATE, display, original, updated)
