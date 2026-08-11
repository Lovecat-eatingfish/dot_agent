"""
文件名 / 目录列表搜索工具

提供快速的文件发现能力，主要特性：
1. glob 模式匹配：支持 **/*.py 这类递归通配符
2. 路径安全：所有结果限制在 workspace 目录内
3. 自动跳过常见无关目录（.git/.venv/__pycache__ 等）
4. 排序与截断：按路径排序，超长结果截断并标记

两种用法：
- GlobTool(pattern="**/*.py"): 按文件名通配符递归搜索文件
- GlobTool(pattern="src", path_type="dir"): 按目录名查找目录
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.file_tools import display_path, resolve_workspace_path

# 递归搜索时跳过的目录（与 GrepTool 保持一致）
SKIP_DIRS = {".git", ".mokioclaw", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".idea", ".workbuddy"}

# 默认最大返回条目数，防止结果过长
DEFAULT_HEAD_LIMIT = 200


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def glob_search(
    state: RuntimeState,
    pattern: str,
    path: str = ".",
    path_type: str = "file",
    head_limit: int | str = DEFAULT_HEAD_LIMIT,
) -> dict[str, Any]:
    """按 glob 模式搜索 workspace 内的文件或目录

    Args:
        state: 运行时状态
        pattern: glob 模式，例如 "**/*.py"、"*.md"、"src/**/*.json"
        path: 搜索根目录（相对于 workspace），默认 "."
        path_type: "file" 只返回文件，"dir" 只返回目录
        head_limit: 最大返回条目数

    Returns:
        结果字典：
        - ok: 是否成功
        - pattern: 使用的模式
        - path: 搜索根目录
        - matches: 匹配的相对路径列表（已排序）
        - truncated: 是否因超过 head_limit 而截断
    """
    if not pattern:
        return {"ok": False, "error": "pattern must not be empty"}
    try:
        head_limit_value = int(head_limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "head_limit must be an integer"}
    if head_limit_value <= 0:
        return {"ok": False, "error": "head_limit must be > 0"}

    try:
        root = resolve_workspace_path(state, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not root.exists():
        return {"ok": False, "error": f"path does not exist: {display_path(state, root)}"}

    want_files = path_type == "file"
    want_dirs = path_type == "dir"
    if not (want_files or want_dirs):
        return {"ok": False, "error": 'path_type must be "file" or "dir"'}

    matches: list[str] = []
    try:
        for candidate in root.glob(pattern):
            if _is_skipped(candidate):
                continue
            if want_files and not candidate.is_file():
                continue
            if want_dirs and not candidate.is_dir():
                continue
            if root == state.workspace.resolve() and candidate == root:
                continue
            matches.append(display_path(state, candidate))
            if len(matches) >= head_limit_value:
                matches.sort()
                return {
                    "ok": True,
                    "pattern": pattern,
                    "path": display_path(state, root),
                    "path_type": path_type,
                    "matches": matches,
                    "truncated": True,
                }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"glob failed: {exc}"}

    matches.sort()
    return {
        "ok": True,
        "pattern": pattern,
        "path": display_path(state, root),
        "path_type": path_type,
        "matches": matches,
        "truncated": False,
    }
