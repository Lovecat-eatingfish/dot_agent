import re

from core import RuntimeState
from typing import Any
import fnmatch
from pathlib import Path

from tools.file_tool import resolve_workspace_path, read_text_lossy, display_path

# 要跳过grep的的目录
SKIP_DIRS = {".git", ".mokioclaw", ".venv", "__pycache__", ".pytest_cache"}


def grep(state: RuntimeState,
         pattern: str,
         path: str = ".",
         glob: str | None = None,
         head_limit: int | str = 50,
         ignore_case: bool = False,
         ) -> dict[str, Any]:
    """

    pattern：要搜索的正则表达式模式

    path：搜索路径（文件或目录），默认为当前目录 .

    glob：文件名匹配模式（如 *.py），用于过滤文件

    head_limit：返回的最大匹配数，默认50条

    ignore_case：是否忽略大小写
    """
    # 参数校验
    if not pattern:
        return {"ok": False, "error": "pattern must not be empty"}
    try:
        head_limit_value = int(head_limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "head_limit must be an integer"}
    if head_limit_value <= 0:
        return {"ok": False, "error": "head_limit must be > 0"}

    root = resolve_workspace_path(state, path)
    candidates: list[Path] = []
    if root.is_file():
        candidates = [root]
    elif root.is_dir():
        candidates = _iter_files(root, glob)

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    matches: list[dict[str, Any]] = []
    for file in candidates:
        lines = read_text_lossy(file).splitlines()  # 读取文件所有行
        for idx, line in enumerate(lines, start=1):  # 逐行检查
            if regex.search(line):  # 正则匹配
                matches.append({
                    "path": display_path(state, file),  # 相对路径
                    "line": idx,  # 行号
                    "text": line  # 匹配的行内容
                })
                if len(matches) >= head_limit_value:  # 达到限制，提前返回
                    return {"ok": True, "pattern": pattern, "matches": matches, "truncated": True}

    return {
        "ok": True,
        "pattern": pattern,
        "matches": matches,  # 匹配结果列表
        "truncated": False  # 是否被截断（达到 limit）
    }


def _iter_files(root: Path, glob_pattern: str | None) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        # fnmatch 是 Python 标准库中的一个模块，用于 Unix shell 风格的通配符模式匹配。
        # 它提供了类似于 Unix/Linux 命令行中 *、?、[] 等通配符的匹配功能。
        if glob_pattern and not fnmatch.fnmatch(path.name, glob_pattern) and not fnmatch.fnmatch(str(path),
                                                                                                 glob_pattern):
            continue
        files.append(path)
    return files
