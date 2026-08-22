"""
Bash 命令执行工具（dot 独立副本）

提供安全的 Shell 命令执行能力：
1. 平台自适应：自动检测 Windows/macOS/Linux 并调整命令语法
2. 安全防护：危险命令检测 + 人工审批机制
3. 超时控制：防止长时间运行的命令阻塞 Agent
4. 输出截断：超长输出自动保存到文件
5. 后台执行：支持长时间运行的命令

相比旧版裁剪掉：LLM 审批分类器、contextModifier、sandbox 模块。
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

from ..core.log import get_logger
from ..core.runtime import RuntimeState
from ..core.utils import BashResult, coerce_bool

logger = get_logger(__name__)

# 后台进程注册表（进程内），用于退出时清理
_background_processes: list[subprocess.Popen] = []

# 输出文件保留天数
_OUTPUT_RETENTION_DAYS = 3

# ========== 默认配置常量 ==========
DEFAULT_TIMEOUT_SECONDS = 120      # 单次命令默认超时（秒）
DEFAULT_MAX_TIMEOUT_SECONDS = 600  # 最大允许超时（秒）
DEFAULT_MAX_OUTPUT_CHARS = 6000    # 输出截断阈值（字符）

# ========== 危险命令模式列表 ==========
DANGEROUS_PATTERNS = [
    r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b",  # Unix 递归强制删除（-rf/-fr/-rvf 等组合标志）
    r"\brm\b(?=[^|;&]*\s-[a-zA-Z]*r)(?=[^|;&]*\s-[a-zA-Z]*f)",  # rm 分离标志 -r ... -f 任意顺序
    r"\brm\b(?=[^|;&]*--recursive)(?=[^|;&]*--force)",   # rm --recursive ... --force 任意顺序
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",       # PowerShell 递归删除
    r"\b(?:del|rmdir)\s+/[sq]\b",                       # Windows 静默删除（含 rmdir /s /q）
    r"\bformat\s+(?:[a-zA-Z]:|/q)",                     # 格式化磁盘（需盘符或 /q，避免误伤 --pretty=format:）
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
    r"\|\s*(?:ba|z|da|k)?sh\b",                          # pipe 到任意 shell（bash/zsh/dash/ksh）
]


def _validate_bash_args(command: str, timeout_seconds: int | None = None) -> list[str]:
    """验证 BashTool 参数的合法性"""
    errors = []
    if not command or not command.strip():
        errors.append("Command must not be empty")
    if len(command) > 10000:
        errors.append(f"Command too long ({len(command)} chars), max 10000")
    if timeout_seconds is not None:
        try:
            timeout = int(timeout_seconds)
            if timeout < 1:
                errors.append(f"timeout_seconds must be >= 1, got {timeout}")
            elif timeout > DEFAULT_MAX_TIMEOUT_SECONDS:
                errors.append(f"timeout_seconds must be <= {DEFAULT_MAX_TIMEOUT_SECONDS}, got {timeout}")
        except (TypeError, ValueError):
            errors.append(f"Invalid timeout_seconds: {timeout_seconds}")
    return errors


def bash_tool_description() -> str:
    """生成 BashTool 的详细使用说明（按平台动态调整）"""
    system = platform.system().lower()

    common = """Execute a shell command inside the workspace with automatic timeout and output capture.

**Platform**: {platform}

**Key behaviors**:
- CWD is set to the workspace automatically. Use relative paths only.
  ❌ Wrong: cd /workspace && python app.py
  ✅ Right: python app.py
- Fresh shell per call: exported env vars do NOT persist between calls.
  Write reusable env to .mokioclaw.env or use inline export.
- Long-running servers (uvicorn, flask run) must use run_in_background=true.
- Output truncated to 6000 chars; full output saved to .mokioclaw/bash-outputs/ if needed.
- Timeout: 120s default, 600s max. Set timeout_seconds for longer tasks.

**Common use cases**:
- Run tests: pytest -q, python -m pytest
- Run scripts: python script.py, node app.js
- Check files: ls, dir, type, cat
- Search files: grep "pattern" *.py
- Install deps: pip install package
- Git operations: git status, git diff

**Output format**:
- ok: boolean (true if exit_code == 0)
- command: normalized command string
- exit_code: process exit code
- stdout/stderr: captured output (truncated if needed)
- stdout_path/stderr_path: path to full output file (if truncated)
- timed_out: true if command exceeded timeout
- duration_ms: execution time in milliseconds

**Security**:
- Dangerous commands (rm -rf, format, shutdown) are BLOCKED.
- Permission gating (mode rules + blacklists) is enforced by PermissionManager before this tool runs.
""".format(platform={
    "windows": "Windows (cmd.exe)",
    "darwin": "macOS (POSIX shell)",
    "linux": "Linux/Unix (POSIX shell)"
}.get(system, "Unknown"))

    if system == "windows":
        return common + """**Windows cmd.exe syntax**:
- List files: dir
- Print file: type file.txt
- Chain commands: command1 && command2
- Set env var: set VAR=value
- Copy/move: copy src.txt dst.txt, move old.txt new.txt
- Delete: del file.txt

❌ Avoid POSIX-only: tail, grep, sed, awk, ls, export, here-documents
✅ Use Python one-liners instead: python -c "import sys; print(open('file.txt').readlines()[-5:])"
"""

    return common + """**POSIX shell syntax** (macOS/Linux):
- List files: ls, ls -la
- Print file: cat file.txt, head -n 10 file.txt
- Search: grep "pattern" *.py
- Chain: command1 && command2, command1; command2
- Set env var: export VAR=value
- Read last N lines: tail -n 10 file.txt
"""


def _coerce_timeout(timeout_seconds: int | str | float) -> int:
    """将超时值转换为整数，无效值返回默认值"""
    if timeout_seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return int(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _normalize_command(command: str) -> str:
    """规范化命令字符串，处理平台差异

    Windows 平台自动转换：
    - python3 → python
    - mkdir -p → mkdir（cmd 的 mkdir 本身就递归）
    - ls → dir、cat → type
    - 移除无效的 cd /workspace
    """
    if os.name == "nt":
        command = re.sub(r"\bmkdir\s+(?:-p|--parents)(?=\s)", "mkdir", command, flags=re.IGNORECASE)
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
        # lookahead 限定 ls 后跟空白/结尾：\b 会把 git ls-files 误改成 git dir-files
        normalized = re.sub(r"\bls\b(?=\s|$)", "dir", normalized)
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
    """处理 tail 命令，转换为 Python 实现，确保跨平台兼容"""
    match = re.fullmatch(r"\s*tail(?:\s+-n)?\s+(\d+)\s+(.+?)\s*", command)
    if not match:
        match = re.fullmatch(r"\s*tail\s+-(\d+)\s+(.+?)\s*", command)
    if not match:
        return None
    count = int(match.group(1))
    raw_path = shlex.split(match.group(2), posix=False)[0]
    from .file_tools import read_text_lossy, resolve_workspace_path

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
    cwd = _effective_cwd(state)
    return {
        "ok": True,
        "timed_out": False,
        "command": command.strip() or "cd",
        "exit_code": 0,
        "stdout": f"{cwd}\n",
        "stderr": "",
        "duration_ms": 0,
    }


def _looks_dangerous(command: str) -> str | None:
    """检查命令是否匹配危险模式"""
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

    流程：参数校验 → 命令规范化 → 特殊命令（tail/cd）→
    危险命令检测 → 人工审批 → 执行 → 输出截断
    """
    validation_errors = _validate_bash_args(command, timeout_seconds)
    if validation_errors:
        return {
            "ok": False,
            "error": "validation_failed",
            "error_message": "; ".join(validation_errors),
            "tool": "BashTool",
            "command": command,
            "hint": "Check command syntax and timeout value",
        }

    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}
    max_timeout = _state_int(state, "bash_max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    if timeout_seconds is None:
        timeout = _state_int(state, "bash_default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    else:
        timeout = _coerce_timeout(timeout_seconds)
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

    started = time.perf_counter()
    env, env_error = _build_env(state)
    if env_error is not None:
        return {"ok": False, "error": env_error}
    max_output_chars = _state_int(state, "bash_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
    bash_cwd = _effective_cwd(state)
    if background:
        return _run_background(state, normalized_command, env)
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            normalized_command,
            cwd=bash_cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # 超时杀整棵进程树：只杀外壳（cmd.exe/sh）会让子进程残留并持有文件锁
        if proc is not None:
            _kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            **_format_captured_output(state, _decode_output(exc.stdout), _decode_output(exc.stderr), max_output_chars),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }

    output = _format_captured_output(state, _decode_output(stdout_bytes), _decode_output(stderr_bytes), max_output_chars)
    return {
        "ok": proc.returncode == 0,
        "timed_out": False,
        "command": normalized_command,
        "exit_code": proc.returncode,
        **output,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def _effective_cwd(state: RuntimeState) -> Path:
    cwd = getattr(state, "cwd", None)
    if cwd is not None:
        try:
            path = Path(cwd)
            if path.is_dir():
                return path
        except OSError:
            pass
    return state.workspace


# ========== 只读 BashTool 白名单 ==========
# verifier 专用：只允许不会修改文件系统的命令。
# 注意：python/node 不在此列——它们可以执行任意代码，不属于"只读"。
READ_ONLY_ALLOWED = {
    "cat", "head", "tail", "grep", "egrep", "fgrep",
    "ls", "dir", "find", "wc",
    "echo", "printf", "which", "where", "whereis",
    "test", "true", "false",
    "uname", "whoami", "pwd", "date",
}


def run_bash_read_only(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | str | float | None = None,
    run_in_background: bool | str = False,
) -> BashResult:
    """只读 Bash 命令执行（verifier 专用）

    只允许白名单命令，拒绝一切可能修改文件系统的操作。
    不走审批流程——只读命令天然安全。
    """
    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}

    normalized_command = _normalize_command(command)

    # 提取第一个 token（处理 env var 前缀如 PYTHONIOENCODING=utf-8 python ...）
    try:
        tokens = shlex.split(normalized_command)
    except ValueError:
        tokens = normalized_command.split()
    if not tokens:
        return {"ok": False, "error": "empty command"}

    cmd_name = ""
    for token in tokens:
        if "=" in token and token[0] != "=" and not token.startswith("-"):
            continue
        cmd_name = token
        break
    cmd_base = Path(cmd_name).stem.lower() if cmd_name else ""
    if cmd_base.endswith(".exe"):
        cmd_base = cmd_base[:-4]

    if cmd_base not in READ_ONLY_ALLOWED:
        return {
            "ok": False,
            "error": f"read-only mode: command '{cmd_base}' is not in the allowed list ({', '.join(sorted(READ_ONLY_ALLOWED))})",
        }

    # 白名单只校验第一个 token，元字符（&& ; | > 等）可拼接任意后续命令——
    # 只读保证必须整条命令无链式/重定向元字符
    _meta = next((ch for ch in ("&&", "||", ";", "|", ">", "<", "`", "$(") if ch in normalized_command), None)
    if _meta is not None:
        return {
            "ok": False,
            "error": f"read-only mode: shell metacharacter '{_meta}' is not allowed; run a single read-only command without chaining or redirection",
        }

    if timeout_seconds is None:
        timeout = _state_int(state, "bash_default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    else:
        timeout = _coerce_timeout(timeout_seconds)
    max_timeout = _state_int(state, "bash_max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    if timeout <= 0:
        return {"ok": False, "error": f"timeout must be > 0 (got {timeout_seconds})"}
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
    env_file = state.bash_env_file or state.workspace / ".dot.env"
    if env_file.exists():
        try:
            env.update(_parse_env_file(env_file, env))
        except OSError as exc:
            return env, f"failed to read bash env file {env_file}: {exc}"
    # 额外注入的环境变量
    extra = getattr(state, "extra_env", None)
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    # env file 可能覆盖 PATH；重新前置 harness 路径保证优先
    _prepend_harness_paths(state, env)
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
    """创建 python/pip shim，保证命令用的是当前解释器"""
    shim_dir = state.workspace / ".dot" / "shims"
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
    output_dir = state.workspace / ".dot" / "bash-outputs"
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
) -> BashResult:
    output_dir = state.workspace / ".dot" / "background"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    stdout_path = output_dir / f"job-{stamp}.out"
    stderr_path = output_dir / f"job-{stamp}.err"
    stdout_handle = stdout_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=_effective_cwd(state),
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
    _background_processes.append(process)
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
    }


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """杀掉整个进程树（shell=True 时 terminate/kill 只杀外壳，孙进程会残留持有文件锁）"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            import signal as _signal

            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def cleanup_background_processes() -> int:
    """终止所有后台进程，返回清理数量"""
    killed = 0
    for proc in _background_processes:
        if proc.poll() is None:
            try:
                _kill_process_tree(proc)
                proc.wait(timeout=5)
                killed += 1
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                    killed += 1
                except OSError:
                    pass
    _background_processes.clear()
    return killed


def cleanup_old_outputs(workspace: Path, *, days: int = _OUTPUT_RETENTION_DAYS) -> int:
    """清理超过指定天数的 bash 输出文件和后台日志"""
    removed = 0
    cutoff = time.time() - days * 86400
    for subdir in ("bash-outputs", "background"):
        output_dir = workspace / ".dot" / subdir
        if not output_dir.exists():
            continue
        for path in output_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.debug("failed to clean output file %s: %s", path, exc)
    return removed
