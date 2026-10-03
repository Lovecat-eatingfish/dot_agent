"""
dot.coding.review.fsutil — 文件变更评审的共享文件系统/差异工具

previewer（事前预览）与 ledger（事后台账）共用：
- 路径解析必须与 file_tools 完全一致（否则预览的就是另一个文件了），
  因此直接复用 dot.coding.tools.file_tools 的解析/展示/解码函数。
"""
from __future__ import annotations

import difflib
from pathlib import Path

from dot.coding.tools.file_tools import (
    _display_path,
    _resolve_path,
    decode_text_lossy,
)

# 审批弹窗 / 终端展示的 diff 截断阈值
MAX_PREVIEW_LINES = 200
MAX_PREVIEW_CHARS = 8000
# 内存快照上限：超过后该记录标记 fallback_git，回滚走 SessionGit
MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_SESSION_BYTES = 20 * 1024 * 1024


def resolve(workspace: Path, file_path: str) -> Path:
    """与 file_tools 一致的路径解析（含 workspace/ 前缀剥离）"""
    return _resolve_path(workspace, file_path)


def display_path(workspace: Path, path: Path) -> str:
    return _display_path(workspace, path)


def is_probably_binary(data: bytes) -> bool:
    """前 8KB 出现 NUL 字节即视为二进制（diff 无意义，审批框退化为摘要展示）"""
    return b"\x00" in data[:8192]


def unified_diff(before: str, after: str, display: str) -> str:
    """unified diff（与 file_tools 产出的格式一致）"""
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{display}", tofile=f"b/{display}",
        lineterm="",
    ))


def diff_stat(diff: str) -> tuple[int, int]:
    """从 unified diff 统计 (+, -) 行数（忽略文件头）"""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def truncate_diff(
    diff: str,
    max_lines: int = MAX_PREVIEW_LINES,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> tuple[str, bool]:
    """按行数/字符数截断，返回 (截断文本, 是否发生截断)"""
    if len(diff) <= max_chars and diff.count("\n") + 1 <= max_lines:
        return diff, False
    lines = diff.splitlines()[:max_lines]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text, True
