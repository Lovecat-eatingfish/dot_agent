"""
Bash 命令执行工具

提供安全的 Shell 命令执行能力，主要特性：
1. 平台自适应：自动检测 Windows/macOS/Linux 并调整命令语法
2. 安全防护：危险命令检测 + 人工审批机制
3. 超时控制：防止长时间运行的命令阻塞 Agent
4. 输出截断：超长输出自动保存到文件，避免上下文溢出
5. 后台执行：支持长时间运行的命令（如开发服务器）

安全机制：
- DANGEROUS_PATTERNS: 危险命令正则匹配列表
- approval_mode: inline（人工确认）/ auto（自动批准）/ deny（自动拒绝）
- 路径限制：所有命令都在 workspace 目录内执行
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mokioclaw.core.approval import ApprovalDecision, classify_command_risk, make_approval_request
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.utils import BashResult, coerce_bool

# ========== 默认配置常量 ==========
DEFAULT_TIMEOUT_SECONDS = 120      # 单次命令默认超时（秒）
DEFAULT_MAX_TIMEOUT_SECONDS = 600  # 最大允许超时（秒）
DEFAULT_MAX_OUTPUT_CHARS = 6000    # 输出截断阈值（字符）

# ========== 危险命令模式列表 ==========
# 匹配到这些模式的命令会被阻止或要求人工审批
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",                                    # Unix 递归删除
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",       # PowerShell 递归删除
    r"\bdel\s+/[sq]\b",                                 # Windows 静默删除
    r"\bformat\b",                                       # 格式化磁盘
    r"\bshutdown\b",                                     # 关机
    r"\breboot\b",                                       # 重启
    r"(?:^|[^0-9])>\s*(?:[A-Za-z]:\\|/(?!dev/null\b))", # 重定向到非 /dev/null
    r"\bmkfs\b",                                         # 创建文件系统
    r"\bdd\s+",                                          # 磁盘镜像写入
    r"\bchmod\s+777\b",                                  # 全开权限
    r"\bchown\b",                                        # 修改所有者
    r"\bkill\s+-9\b",                                    # 强制杀进程
    r"\bpkill\b",                                        # 按名杀进程
    r"\biptables\b",                                     # 防火墙操作
    r"(?:^|[;&|])\s*eval\s",                             # eval 执行
    r"(?:^|[;&|])\s*exec\s",                             # exec 替换进程
    r"\|\s*(ba)?sh\b",                                   # pipe 到 shell
]


def bash_tool_description() -> str:
    """生成 BashTool 的使用说明，根据当前平台动态调整

    Returns:
        包含平台特定说明的工具描述字符串
    """
    system = platform.system().lower()
    common = (
        "Run a safe development shell command inside the workspace with timeout and output capture. "
        "The command already runs with cwd set to the workspace, so use relative paths and do not run cd /workspace, "
        "cd workspace, or long-lived interactive commands. Each call starts a fresh shell; exported variables do not persist "
        "between calls, so write reusable environment values to the configured env file or pass them inline. "
        "Long-running servers should use run_in_background=true. Prefer cross-platform Python one-liners for file checks."
    )
    if system == "windows":
        return (
            common
            + " Current platform: Windows. Commands are executed by cmd.exe, not bash or PowerShell. "
            "Use Windows cmd syntax: dir for listing, type file.txt for printing a file, copy/move/del for simple file operations, "
            "&& for chaining, and set VAR=value for environment variables. Do not use POSIX-only tools like tail, grep, sed, awk, "
            "cat, ls, export, or here-documents unless you implement the behavior with python -c."
        )
    if system == "darwin":
        return (
            common
            + " Current platform: macOS. Commands are executed by a POSIX shell. "
            "Use portable sh/bash-style commands such as ls, cat, grep, tail, export, and python/python3 as available."
        )
    return (
        common
        + " Current platform: Linux/Unix. Commands are executed by a POSIX shell. "
        "Use portable sh/bash-style commands such as ls, cat, grep, tail, export, and python/python3 as available."
    )


def _coerce_timeout(timeout_seconds: int | str | float) -> int:
    """将超时值转换为整数，无效值返回默认值

    Args:
        timeout_seconds: 超时值，支持 int/str/float

    Returns:
        整数类型的超时秒数
    """
    if timeout_seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return int(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _normalize_command(command: str) -> str:
    """规范化命令字符串，处理平台差异

    Windows 平台会自动转换：
    - python3 → python
    - ls → dir
    - cat → type
    - 移除无效的 cd /workspace

    Args:
        command: 原始命令字符串

    Returns:
        规范化后的命令
    """
    if os.name == "nt":
        normalized = re.sub(r"^\s*python3(\.exe)?\b", "python", command, count=1, flags=re.IGNORECASE)
        normalized = re.sub(
            r"^\s*cd\s+(?:/workspace|workspace|\.?/workspace|\.mokioclaw[\\/]+workspace)\s*(?:&&|&)\s*",
            "",
            normalized,
            count=1,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"^\s*pwd\s*$", "cd", normalized, count=1, flags=re.IGNORECASE)
        normalized = re.sub(r"\bls\s+-la\b", "dir", normalized)
        normalized = re.sub(r"\bls\b", "dir", normalized)
        normalized = re.sub(r"\bcat\s+([^\s|&<>]+)", r"type \1", normalized)
        return normalized
    return re.sub(
        r"^\s*cd\s+(?:/workspace|workspace|\.?/workspace|\.mokioclaw[\\/]+workspace)\s*(?:&&|;)\s*pwd\s*$",
        "cd",
        command,
        count=1,
        flags=re.IGNORECASE,
    )


def _handle_tail_command(state: RuntimeState, command: str) -> BashResult | None:
    """处理 tail 命令，避免在 Windows 上执行不支持的命令

    将 tail -N file.txt 转换为 Python 实现，确保跨平台兼容。

    Args:
        state: 运行时状态
        command: 命令字符串

    Returns:
        命令执行结果，如果不匹配 tail 格式则返回 None
    """
    match = re.fullmatch(r"\s*tail(?:\s+-n)?\s+(\d+)\s+(.+?)\s*", command)
    if not match:
        match = re.fullmatch(r"\s*tail\s+-(\d+)\s+(.+?)\s*", command)
    if not match:
        return None
    count = int(match.group(1))
    raw_path = shlex.split(match.group(2), posix=False)[0]
    from mokioclaw.tools.file_tools import read_text_lossy, resolve_workspace_path

    path = resolve_workspace_path(state, raw_path)
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"file does not exist: {raw_path}"}
    lines = read_text_lossy(path).splitlines()
    output = "\n".join(lines[-count:])
    return {
        "ok": True,
        "timed_out": False,
        "command": command,
        "exit_code": 0,
        "stdout": output + ("\n" if output else ""),
        "stderr": "",
        "duration_ms": 0,
    }


def _handle_workspace_query(state: RuntimeState, command: str) -> BashResult | None:
    if not re.fullmatch(r"\s*(?:cd|pwd)\s*", command, flags=re.IGNORECASE):
        return None
    return {
        "ok": True,
        "timed_out": False,
        "command": command.strip() or "cd",
        "exit_code": 0,
        "stdout": f"{state.workspace}\n",
        "stderr": "",
        "duration_ms": 0,
    }


def _looks_dangerous(command: str) -> str | None:
    """检查命令是否匹配危险模式

    Args:
        command: 命令字符串

    Returns:
        匹配到的危险模式，如果不危险则返回 None
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


def _decode_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return output.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return output.decode("utf-8", errors="replace")


def run_bash(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | str | float | None = None,
    run_in_background: bool | str = False,
) -> BashResult:
    """执行 Bash 命令

    核心执行流程：
    1. 参数校验和超时计算
    2. 命令规范化（处理平台差异）
    3. 特殊命令处理（tail、cd/pwd）
    4. 危险命令检测
    5. 人工审批（如果需要）
    6. 执行命令并捕获输出
    7. 输出截断处理

    Args:
        state: 运行时状态，包含工作区路径和配置
        command: 要执行的命令字符串
        timeout_seconds: 超时时间（秒），None 使用默认值
        run_in_background: 是否后台执行

    Returns:
        命令执行结果字典，包含：
        - ok: 是否成功
        - command: 实际执行的命令
        - exit_code: 退出码
        - stdout: 标准输出
        - stderr: 标准错误
        - duration_ms: 执行耗时（毫秒）
    """
    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}
    max_timeout = _state_int(state, "bash_max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    timeout = _coerce_timeout(timeout_seconds)
    if timeout_seconds is None:
        timeout = _state_int(state, "bash_default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if timeout <= 0 or timeout > max_timeout:
        return {"ok": False, "error": f"timeout_seconds must be between 1 and {max_timeout}"}
    normalized_command = _normalize_command(command)
    background = coerce_bool(run_in_background)

    handled = _handle_tail_command(state, normalized_command)
    if handled is not None:
        return handled
    handled = _handle_workspace_query(state, normalized_command)
    if handled is not None:
        return handled

    blocked = _looks_dangerous(normalized_command)
    if blocked:
        return {"ok": False, "error": f"blocked potentially dangerous command pattern: {blocked}"}

    approval = _resolve_approval(state, normalized_command)
    if approval is not None and not approval.get("approved"):
        return approval

    started = time.perf_counter()
    env, env_error = _build_env(state)
    if env_error is not None:
        return {"ok": False, "error": env_error}
    max_output_chars = _state_int(state, "bash_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
    if background:
        return _run_background(state, normalized_command, env, approval)
    try:
        completed = subprocess.run(
            normalized_command,
            cwd=state.workspace,
            shell=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            **_format_captured_output(state, _decode_output(exc.stdout), _decode_output(exc.stderr), max_output_chars),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            **(approval or {}),
        }

    output = _format_captured_output(state, _decode_output(completed.stdout), _decode_output(completed.stderr), max_output_chars)
    return {
        "ok": completed.returncode == 0,
        "timed_out": False,
        "command": normalized_command,
        "exit_code": completed.returncode,
        **output,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        **(approval or {}),
    }


# ========== 只读 BashTool 白名单 ==========
# verifier 专用：只允许不会修改文件系统的命令
READ_ONLY_ALLOWED = {
    "cat", "head", "tail", "grep", "egrep", "fgrep",
    "ls", "dir", "find", "wc", "wcw",
    "echo", "printf", "which", "where", "whereis",
    "test", "true", "false",
    "python", "python3", "node",
    "uname", "whoami", "pwd", "date",
}


def run_bash_read_only(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | str | float | None = None,
    run_in_background: bool | str = False,
) -> BashResult:
    """只读 Bash 命令执行（verifier 专用）

    只允许白名单中的命令，拒绝一切可能修改文件系统的操作。
    不走审批流程——只读命令天然安全。
    """
    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}

    normalized_command = _normalize_command(command)

    # 提取命令的第一个 token（处理 env var 前缀如 PYTHONIOENCODING=utf-8 python ...）
    try:
        tokens = shlex.split(normalized_command)
    except ValueError:
        tokens = normalized_command.split()
    if not tokens:
        return {"ok": False, "error": "empty command"}

    # 跳过 VAR=val 前缀
    cmd_name = ""
    for token in tokens:
        if "=" in token and token[0] != "=" and not token.startswith("-"):
            continue
        cmd_name = token
        break
    cmd_base = Path(cmd_name).stem.lower() if cmd_name else ""
    # Windows 上 python.exe → python
    if cmd_base.endswith(".exe"):
        cmd_base = cmd_base[:-4]

    if cmd_base not in READ_ONLY_ALLOWED:
        return {
            "ok": False,
            "error": f"read-only mode: command '{cmd_base}' is not in the allowed list ({', '.join(sorted(READ_ONLY_ALLOWED))})",
        }

    timeout = _coerce_timeout(timeout_seconds)
    if timeout_seconds is None:
        timeout = _state_int(state, "bash_default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    max_timeout = _state_int(state, "bash_max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    if timeout <= 0 or timeout > max_timeout:
        timeout = min(timeout, max_timeout)

    started = time.perf_counter()
    env, env_error = _build_env(state)
    if env_error is not None:
        return {"ok": False, "error": env_error}
    max_output_chars = _state_int(state, "bash_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)

    try:
        completed = subprocess.run(
            normalized_command,
            cwd=state.workspace,
            shell=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            **_format_captured_output(state, _decode_output(exc.stdout), _decode_output(exc.stderr), max_output_chars),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }

    output = _format_captured_output(state, _decode_output(completed.stdout), _decode_output(completed.stderr), max_output_chars)
    return {
        "ok": completed.returncode == 0,
        "timed_out": False,
        "command": normalized_command,
        "exit_code": completed.returncode,
        **output,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def _resolve_approval(state: RuntimeState, command: str) -> BashResult | None:
    risk_reason = classify_command_risk(command)
    if risk_reason is None:
        return None

    request = make_approval_request(command, risk_reason)
    base = {
        "requires_approval": True,
        "approval_id": request.id,
        "risk_reason": risk_reason,
        "command": command,
    }
    if state.approval_mode == "auto":
        return {**base, "approved": True}
    if state.approval_mode == "deny" or state.approval_handler is None:
        return {
            **base,
            "ok": False,
            "approved": False,
            "error": f"human approval required for high-risk command: {risk_reason}",
        }

    decision = state.approval_handler(request)
    if isinstance(decision, ApprovalDecision):
        approved = decision.approved
        decision_reason = decision.reason
    else:
        approved = bool(decision)
        decision_reason = ""
    if approved:
        return {**base, "approved": True}
    return {
        **base,
        "ok": False,
        "approved": False,
        "error": decision_reason or f"human rejected high-risk command: {risk_reason}",
    }


def _state_int(state: RuntimeState, name: str, default: int) -> int:
    try:
        value = int(getattr(state, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _build_env(state: RuntimeState) -> tuple[dict[str, str], str | None]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    _prepend_harness_paths(state, env)
    env_file = state.bash_env_file or state.workspace / ".mokioclaw.env"
    if env_file.exists():
        try:
            env.update(_parse_env_file(env_file, env))
        except OSError as exc:
            return env, f"failed to read bash env file {env_file}: {exc}"
    return env, None


def _prepend_harness_paths(state: RuntimeState, env: dict[str, str]) -> None:
    path_candidates = [
        _ensure_toolchain_shims(state),
        state.workspace / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
        state.workspace / "venv" / ("Scripts" if os.name == "nt" else "bin"),
        state.workspace / "node_modules" / ".bin",
        Path(sys.executable).parent,
    ]
    existing = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for path in [str(candidate) for candidate in path_candidates if candidate.exists()] + existing:
        if path not in merged:
            merged.append(path)
    env["PATH"] = os.pathsep.join(merged)
    if getattr(sys, "prefix", None) and sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        env.setdefault("VIRTUAL_ENV", sys.prefix)


def _ensure_toolchain_shims(state: RuntimeState) -> Path:
    shim_dir = state.workspace / ".mokioclaw" / "shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    python_executable = sys.executable
    if os.name == "nt":
        _write_shim(shim_dir / "python.cmd", f'@echo off\r\n"{python_executable}" %*\r\n')
        _write_shim(shim_dir / "python3.cmd", f'@echo off\r\n"{python_executable}" %*\r\n')
        pip_cmd = (
            "@echo off\r\n"
            f'"{python_executable}" -c "import pathlib,sys; import pip; '
            'p=pathlib.Path(pip.__file__).resolve(); '
            'prefix=pathlib.Path(sys.prefix).resolve(); '
            'raise SystemExit(0 if p == prefix or prefix in p.parents else 1)" >nul 2>nul\r\n'
            f'if errorlevel 1 "{python_executable}" -m ensurepip --upgrade >nul 2>nul\r\n'
            f'"{python_executable}" -m pip %*\r\n'
        )
        _write_shim(shim_dir / "pip.cmd", pip_cmd)
        _write_shim(shim_dir / "pip3.cmd", pip_cmd)
        return shim_dir
    _write_shim(shim_dir / "python", f"#!/bin/sh\nexec {shlex.quote(python_executable)} \"$@\"\n")
    _write_shim(shim_dir / "python3", f"#!/bin/sh\nexec {shlex.quote(python_executable)} \"$@\"\n")
    quoted_python = shlex.quote(python_executable)
    pip_shim = (
        "#!/bin/sh\n"
        f"{quoted_python} - <<'PY' >/dev/null 2>&1\n"
        "import pathlib\n"
        "import sys\n"
        "import pip\n"
        "pip_path = pathlib.Path(pip.__file__).resolve()\n"
        "prefix = pathlib.Path(sys.prefix).resolve()\n"
        "raise SystemExit(0 if pip_path == prefix or prefix in pip_path.parents else 1)\n"
        "PY\n"
        "if [ $? -ne 0 ]; then\n"
        f"  {quoted_python} -m ensurepip --upgrade >/dev/null 2>&1 || exit $?\n"
        "fi\n"
        f"exec {quoted_python} -m pip \"$@\"\n"
    )
    _write_shim(shim_dir / "pip", pip_shim)
    _write_shim(shim_dir / "pip3", pip_shim)
    return shim_dir


def _write_shim(path: Path, content: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != content:
        path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)


def _parse_env_file(path, base_env: dict[str, str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        parsed[key] = _expand_env_value(_unquote_env_value(value.strip()), {**base_env, **parsed})
    return parsed


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _expand_env_value(value: str, env: dict[str, str]) -> str:
    def replace_var(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain") or ""
        return env.get(name, "")

    return re.sub(r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)", replace_var, value)


def _format_captured_output(state: RuntimeState, stdout: str, stderr: str, max_output_chars: int) -> dict[str, str | bool]:
    output: dict[str, Any] = {}
    output_dir = state.workspace / ".mokioclaw" / "bash-outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    if len(stdout) > max_output_chars:
        stdout_path = output_dir / f"stdout-{time.time_ns()}.log"
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        output["stdout_path"] = str(stdout_path.relative_to(state.workspace))
        output["stdout_truncated"] = True
    if len(stderr) > max_output_chars:
        stderr_path = output_dir / f"stderr-{time.time_ns()}.log"
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
        output["stderr_path"] = str(stderr_path.relative_to(state.workspace))
        output["stderr_truncated"] = True
    output["stdout"] = stdout[:max_output_chars]
    output["stderr"] = stderr[:max_output_chars]
    return output


def _run_background(
    state: RuntimeState,
    command: str,
    env: dict[str, str],
    approval: dict[str, Any] | None,
) -> BashResult:
    output_dir = state.workspace / ".mokioclaw" / "background"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    stdout_path = output_dir / f"job-{stamp}.out"
    stderr_path = output_dir / f"job-{stamp}.err"
    stdout_handle = stdout_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=state.workspace,
            shell=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name != "nt"),
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return {
        "ok": True,
        "timed_out": False,
        "command": command,
        "background": True,
        "pid": process.pid,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_path": str(stdout_path.relative_to(state.workspace)),
        "stderr_path": str(stderr_path.relative_to(state.workspace)),
        "duration_ms": 0,
        **(approval or {}),
    }
