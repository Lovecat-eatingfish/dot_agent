"""
文件操作工具集

提供安全的文件读写和编辑能力，主要特性：
1. 路径安全：所有操作都限制在 workspace 目录内
2. 编码容错：自动尝试多种编码（utf-8, gbk 等）
3. 并发保护：通过 FileSnapshot 机制防止覆盖他人修改
4. 差异追踪：写入/编辑操作返回 unified diff

工具列表：
- FileReadTool: 读取文件内容（支持 offset/limit 分页）
- FileWriteTool: 创建或重写文件
- FileEditTool: 精确替换文件中的文本片段
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from mokioclaw.state.runtime import RuntimeState
from mokioclaw.core.utils import FileEditResult, FileReadResult, FileWriteResult
from mokioclaw.security.path_security import PathAccessDeniedError, PathSecurityError, PathTraversalError

# 单次读取的最大行数，防止读取超大文件导致内存溢出
MAX_READ_LINES = 2000

# 尝试的编码顺序，优先 UTF-8，兼容 Windows 中文环境
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


def _strip_workspace_prefix(file_path: str) -> str:
    """移除路径中的 workspace/ 前缀

    LLM 有时会在路径前添加 workspace/，这个函数负责清理这种冗余前缀。

    Args:
        file_path: 原始路径字符串

    Returns:
        清理后的路径
    """
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
    """容错读取文本文件，自动尝试多种编码

    按照 TEXT_ENCODINGS 的顺序尝试解码，如果都失败则使用 replace 策略。

    Args:
        path: 文件路径

    Returns:
        文件内容字符串
    """
    last_error: UnicodeDecodeError | None = None
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def resolve_workspace_path(state: RuntimeState, file_path: str, operation: str = "read") -> Path:
    """将相对路径解析为工作区内的绝对路径

    处理流程：
    1. 移除 workspace/ 前缀
    2. 展开 ~ 用户目录
    3. 相对路径拼接 workspace
    4. 安全检查（必须在 workspace 内，不在黑名单）

    Args:
        state: 运行时状态
        file_path: 文件路径字符串
        operation: 操作类型（"read" / "write" / "delete"）

    Returns:
        解析后的绝对路径

    Raises:
        PathTraversalError: 路径遍历攻击
        PathAccessDeniedError: 访问被拒绝
    """
    raw = Path(_strip_workspace_prefix(file_path)).expanduser()
    if not raw.is_absolute():
        raw = state.workspace / raw
    return state.assert_workspace_path(raw, operation=operation)


def display_path(state: RuntimeState, path: Path) -> str:
    """获取用于显示的相对路径

    将绝对路径转换为相对于工作区的路径，便于在输出中展示。

    Args:
        state: 运行时状态
        path: 文件路径

    Returns:
        相对路径字符串，如果无法计算则返回原路径
    """
    try:
        return str(path.resolve().relative_to(state.workspace.resolve()))
    except ValueError:
        return str(path)


def _validate_write_args(file_path: str, content: str) -> list[str]:
    """验证 FileWriteTool 参数的合法性

    Args:
        file_path: 文件路径
        content: 文件内容

    Returns:
        错误信息列表，空列表表示验证通过
    """
    errors = []

    if not file_path or not file_path.strip():
        errors.append("file_path must not be empty")
    elif len(file_path) > 4096:
        errors.append(f"file_path too long ({len(file_path)} chars), max 4096")

    if content is None:
        errors.append("content must not be None")
    elif len(content) > 10_000_000:  # 10MB
        errors.append(f"content too large ({len(content)} bytes), max 10MB")

    return errors


def _validate_edit_args(file_path: str, old_text: str, new_text: str) -> list[str]:
    """验证 FileEditTool 参数的合法性

    Args:
        file_path: 文件路径
        old_text: 要替换的原文
        new_text: 替换后的新文本

    Returns:
        错误信息列表，空列表表示验证通过
    """
    errors = []

    if not file_path or not file_path.strip():
        errors.append("file_path must not be empty")
    elif len(file_path) > 4096:
        errors.append(f"file_path too long ({len(file_path)} chars), max 4096")

    if not old_text or not old_text.strip():
        errors.append("old_text must not be empty")

    if new_text is None:
        errors.append("new_text must not be None")
    elif len(new_text) > 10_000_000:  # 10MB
        errors.append(f"new_text too large ({len(new_text)} bytes), max 10MB")

    return errors


def read_file(
    state: RuntimeState,
    file_path: str,
    offset: int | str = 0,
    limit: int | str = MAX_READ_LINES,
) -> FileReadResult:
    """读取文件内容（支持分页）

    安全机制：
    - 路径必须在工作区内
    - 路径不在黑名单
    - 自动尝试多种编码
    - 记录文件快照用于后续写入保护

    Args:
        state: 运行时状态
        file_path: 文件路径（相对于工作区）
        offset: 起始行号（从 0 开始）
        limit: 读取的行数

    Returns:
        结果字典，包含：
        - ok: 是否成功
        - path: 文件相对路径
        - total_lines: 文件总行数
        - content: 带行号的文件内容
        - complete: 是否完整读取
    """
    try:
        path = resolve_workspace_path(state, file_path, operation="read")
    except (ValueError, PathSecurityError) as exc:
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


def write_file(state: RuntimeState, file_path: str, content: str) -> FileWriteResult:
    """创建或重写文件

    安全机制：
    - 新文件：直接创建
    - 已有文件：必须先读取，且文件未被修改过

    Args:
        state: 运行时状态
        file_path: 文件路径（相对于工作区）
        content: 要写入的内容

    Returns:
        结果字典，包含：
        - ok: 是否成功
        - type: "create" 或 "update"
        - path: 文件相对路径
        - lines: 写入后的行数
        - diff: unified diff 格式的变更
    """
    # 1. 输入验证
    validation_errors = _validate_write_args(file_path, content)
    if validation_errors:
        return {
            "ok": False,
            "error": "validation_failed",
            "error_message": "; ".join(validation_errors),
            "tool": "FileWriteTool",
            "file_path": file_path,
            "hint": "Check file_path and content size",
        }

    try:
        path = resolve_workspace_path(state, file_path, operation="write")
    except (ValueError, PathSecurityError) as exc:
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


def edit_file(state: RuntimeState, file_path: str, old_text: str, new_text: str) -> FileEditResult:
    """精确编辑文件，替换唯一的文本片段

    安全机制：
    - 必须先读取文件
    - 文件未被修改过
    - old_text 必须唯一匹配

    Args:
        state: 运行时状态
        file_path: 文件路径（相对于工作区）
        old_text: 要替换的原文（必须唯一）
        new_text: 替换后的新文本

    Returns:
        结果字典，包含：
        - ok: 是否成功
        - path: 文件相对路径
        - replacements: 替换次数（总是 1）
        - diff: unified diff 格式的变更
    """
    # 1. 输入验证
    validation_errors = _validate_edit_args(file_path, old_text, new_text)
    if validation_errors:
        return {
            "ok": False,
            "error": "validation_failed",
            "error_message": "; ".join(validation_errors),
            "tool": "FileEditTool",
            "file_path": file_path,
            "hint": "Check file_path, old_text, and new_text",
        }

    try:
        path = resolve_workspace_path(state, file_path, operation="write")
    except (ValueError, PathSecurityError) as exc:
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
