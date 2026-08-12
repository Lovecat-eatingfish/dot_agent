"""
内容搜索工具

提供工作区内正则搜索能力，主要特性：
1. ripgrep 优先：当系统存在 rg 时通过子进程调用以获得毫秒级响应
2. 纯 Python 回退：无 rg 时逐文件扫描，保证可移植性
3. 上下文行：支持 -A/-B/-C 显示匹配前后的行
4. 多种输出模式：content（带行号正文）/ files_with_matches（仅文件名）/ count（计数）
5. 路径安全：所有搜索限制在 workspace 内，自动跳过无关目录

安全机制：
- ripgrep 仅以只读参数运行，禁止执行外部命令
- 路径必须解析到 workspace 内部
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools.file_tools import display_path, read_text_lossy, resolve_workspace_path

SKIP_DIRS = {".git", ".mokioclaw", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".idea", ".workbuddy"}

VALID_OUTPUT_MODES = {"content", "files_with_matches", "count"}

# ripgrep 单次最大匹配条目，防止结果爆炸
_RG_MAX_MATCHES = 200


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _iter_files(root: Path, glob_pattern: str | None) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_skipped(path):
            continue
        if glob_pattern:
            import fnmatch

            if not fnmatch.fnmatch(path.name, glob_pattern) and not fnmatch.fnmatch(str(path), glob_pattern):
                continue
        files.append(path)
    return files


def _coerce_int(value: Any, field: str) -> int | None:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    if ivalue < 0:
        return None
    return ivalue


def grep(
    state: RuntimeState,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    head_limit: int | str = 50,
    ignore_case: bool = False,
    context_before: int | str = 0,
    context_after: int | str = 0,
    output_mode: str = "content",
) -> dict[str, Any]:
    """在 workspace 内按正则搜索文本

    Args:
        state: 运行时状态
        pattern: 正则表达式
        path: 搜索根目录或文件（相对 workspace）
        glob: 文件名通配符过滤，例如 "*.py"
        head_limit: content 模式下返回的最大匹配条目数
        ignore_case: 是否忽略大小写
        context_before: 匹配行前显示的行数
        context_after: 匹配行后显示的行数
        output_mode: content / files_with_matches / count

    Returns:
        结果字典，包含 ok、pattern、matches/files/counts 等
    """
    if not pattern:
        return {"ok": False, "error": "pattern must not be empty"}
    head = _coerce_int(head_limit, "head_limit")
    if head is None:
        return {"ok": False, "error": "head_limit must be a non-negative integer"}
    if head == 0:
        return {"ok": False, "error": "head_limit must be > 0"}
    before = _coerce_int(context_before, "context_before")
    after = _coerce_int(context_after, "context_after")
    if before is None or after is None:
        return {"ok": False, "error": "context_before/context_after must be non-negative integers"}
    mode = (output_mode or "content").strip().lower()
    if mode not in VALID_OUTPUT_MODES:
        return {"ok": False, "error": f"output_mode must be one of: {', '.join(sorted(VALID_OUTPUT_MODES))}"}

    # 先校验正则合法性
    flags = re.IGNORECASE if ignore_case else 0
    try:
        re.compile(pattern, flags)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    try:
        root = resolve_workspace_path(state, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not root.exists():
        return {"ok": False, "error": f"path does not exist: {display_path(state, root)}"}

    rg_path = shutil.which("rg")
    if rg_path:
        result = _grep_with_ripgrep(rg_path, state, root, pattern, glob, head, ignore_case, before, after, mode)
        if result is not None:
            return result

    return _grep_with_python(state, root, pattern, glob, head, ignore_case, before, after, mode, flags)


def _grep_with_ripgrep(
    rg: str,
    state: RuntimeState,
    root: Path,
    pattern: str,
    glob: str | None,
    head_limit: int,
    ignore_case: bool,
    before: int,
    after: int,
    mode: str,
) -> dict[str, Any] | None:
    """尝试用 ripgrep 搜索；任何异常都返回 None 以回退到 Python 实现"""
    cmd = [rg, "--no-config", "--color=never", "--line-number", "--no-heading"]
    for skip in sorted(SKIP_DIRS):
        cmd.extend(["--glob", f"!{skip}"])
    if ignore_case:
        cmd.append("-i")
    if before:
        cmd.extend([f"-B{before}"])
    if after:
        cmd.extend([f"-A{after}"])
    if glob:
        cmd.extend(["--glob", glob])
    if mode == "files_with_matches":
        cmd.append("--files-with-matches")
    elif mode == "count":
        cmd.append("--count-matches")
    cmd.extend(["--max-count", str(head_limit), pattern, str(root)])
    try:
        proc = subprocess.run(
            cmd,
            cwd=state.workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # 1 = 无匹配
        return None

    if mode == "files_with_matches":
        files = [display_path(state, Path(line)) for line in proc.stdout.splitlines() if line.strip()]
        files.sort()
        truncated = False
        if len(files) > head_limit:
            files = files[:head_limit]
            truncated = True
        return {
            "ok": True,
            "pattern": pattern,
            "path": display_path(state, root),
            "output_mode": mode,
            "files": files,
            "truncated": truncated,
            "engine": "ripgrep",
        }
    if mode == "count":
        counts: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            # 格式: path:count  或  path:0
            sep = line.rfind(":")
            if sep == -1:
                continue
            file_part, count_part = line[:sep], line[sep + 1 :]
            try:
                count_value = int(count_part)
            except ValueError:
                continue
            counts.append({"path": display_path(state, Path(file_part)), "count": count_value})
        return {
            "ok": True,
            "pattern": pattern,
            "path": display_path(state, root),
            "output_mode": mode,
            "counts": counts,
            "engine": "ripgrep",
        }

    # content 模式：解析行号输出，按文件分组
    matches = _parse_ripgrep_content(proc.stdout, state, root, head_limit)
    return {
        "ok": True,
        "pattern": pattern,
        "path": display_path(state, root),
        "output_mode": "content",
        "matches": matches[:head_limit],
        "truncated": len(matches) > head_limit,
        "engine": "ripgrep",
    }


def _parse_ripgrep_content(stdout: str, state: RuntimeState, root: Path, head_limit: int) -> list[dict[str, Any]]:
    """解析 ripgrep 的 --line-number --no-heading 输出

    输出形如:
      path:line:text
      path-line-text   (上下文分隔行，--no-heading 模式下用 - 连接)
    """
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        # 上下文行用 path-line-text，匹配行用 path:line:text
        sep = ":" if ":" in line.split("-", 1)[0] else None
        # 简单策略：尝试匹配 path:line: 或 path-line-
        m = re.match(r"^(?P<path>.+?)(?P<sep>[:\-])(?P<lineno>\d+)[:\-](?P<text>.*)$", line)
        if not m:
            continue
        is_context = m.group("sep") == "-"
        matches.append(
            {
                "path": display_path(state, Path(m.group("path"))),
                "line": int(m.group("lineno")),
                "text": m.group("text"),
                "context": is_context,
            }
        )
        if len(matches) >= head_limit * 3:  # 留余量再外部截断
            break
    return matches


def _grep_with_python(
    state: RuntimeState,
    root: Path,
    pattern: str,
    glob: str | None,
    head_limit: int,
    ignore_case: bool,
    before: int,
    after: int,
    mode: str,
    flags: int,
) -> dict[str, Any]:
    regex = re.compile(pattern, flags)
    if root.is_file():
        candidates = [root]
    elif root.is_dir():
        candidates = _iter_files(root, glob)
    else:
        return {"ok": False, "error": f"path does not exist: {display_path(state, root)}"}

    if mode == "files_with_matches":
        files: list[str] = []
        for file in candidates:
            try:
                if regex.search(read_text_lossy(file)):
                    files.append(display_path(state, file))
                    if len(files) >= head_limit:
                        files.sort()
                        return {"ok": True, "pattern": pattern, "path": display_path(state, root), "output_mode": mode, "files": files, "truncated": True, "engine": "python"}
            except OSError:
                continue
        files.sort()
        return {"ok": True, "pattern": pattern, "path": display_path(state, root), "output_mode": mode, "files": files, "truncated": False, "engine": "python"}

    if mode == "count":
        counts: list[dict[str, Any]] = []
        for file in candidates:
            try:
                lines = read_text_lossy(file).splitlines()
            except OSError:
                continue
            count = sum(1 for line in lines if regex.search(line))
            if count:
                counts.append({"path": display_path(state, file), "count": count})
        return {"ok": True, "pattern": pattern, "path": display_path(state, root), "output_mode": mode, "counts": counts, "engine": "python"}

    # content 模式
    matches: list[dict[str, Any]] = []
    truncated = False
    for file in candidates:
        try:
            lines = read_text_lossy(file).splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append({"path": display_path(state, file), "line": idx, "text": line, "context": False})
                # 上下文行（去重由 line 范围保证）
                if before or after:
                    start_ctx = max(0, idx - before - 1)
                    end_ctx = min(len(lines), idx + after)
                    for j in range(start_ctx, end_ctx):
                        if j + 1 == idx:
                            continue
                        matches.append({"path": display_path(state, file), "line": j + 1, "text": lines[j], "context": True})
                if len(matches) >= head_limit:
                    truncated = True
                    return {"ok": True, "pattern": pattern, "path": display_path(state, root), "output_mode": "content", "matches": matches[:head_limit], "truncated": truncated, "engine": "python"}
    return {"ok": True, "pattern": pattern, "path": display_path(state, root), "output_mode": "content", "matches": matches[:head_limit], "truncated": truncated, "engine": "python"}
