"""
dot.coding.tools.bash_tool — Bash 命令执行工具

安全的 Shell 命令执行，注册为 AgentTool frozen dataclass。
"""
from __future__ import annotations

import asyncio
import platform
import subprocess
from collections.abc import Mapping
from pathlib import Path

from dot.ai.types import TextContent
from dot.agent.tools import AgentTool, AgentToolResult, JSONValue

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 6000

# 危险命令正则（最高优先级，不可覆盖）
_DANGEROUS_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--no-preserve-root)\b",  # rm -rf
    r"\bmkfs\b",           # 格式化磁盘
    r"\bdd\s+.*of=/dev/",  # dd 写磁盘
    r"\b:(){ :\|:& };:",   # fork bomb
    r"\bshutdown\b",       # 关机
    r"\breboot\b",         # 重启
    r"\bformat\b",         # Windows 格式化
    r"\bdel\s+/[sfq]",     # Windows 删除
]


def _looks_dangerous(command: str) -> str | None:
    """检测危险命令，返回匹配的 pattern 或 None"""
    import re
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


async def _run_bash(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    state = arguments.get("_state", {})
    workspace = state.get("workspace", Path.cwd())

    command = str(arguments.get("command", ""))
    timeout = int(arguments.get("timeout_seconds", DEFAULT_TIMEOUT))
    timeout = min(max(timeout, 1), MAX_TIMEOUT)

    if not command.strip():
        return AgentToolResult(content=[TextContent(text="Command must not be empty")])

    # 危险命令检测（fail-closed）
    danger = _looks_dangerous(command)
    if danger:
        return AgentToolResult(content=[TextContent(text=f"Blocked: dangerous command pattern detected ({danger})")])

    is_windows = platform.system().lower() == "windows"
    shell_cmd = ["cmd", "/c", command] if is_windows else ["bash", "-c", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(workspace),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentToolResult(content=[TextContent(text=f"Command timed out after {timeout}s")])

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(stdout)} bytes total)"

        exit_code = proc.returncode
        status = "success" if exit_code == 0 else "error"
        return AgentToolResult(
            content=[TextContent(text=output)],
            details={"exit_code": exit_code, "status": status},
        )
    except Exception as exc:
        return AgentToolResult(content=[TextContent(text=f"Bash error: {type(exc).__name__}: {exc}")])


def create_bash_tool(state: dict) -> AgentTool:
    system = platform.system().lower()
    return AgentTool(
        name="bash",
        label="Bash",
        description=f"Execute a shell command ({system}). CWD is workspace. Timeout: {DEFAULT_TIMEOUT}s max {MAX_TIMEOUT}s.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout_seconds": {"type": "integer", "description": f"Timeout (1-{MAX_TIMEOUT})", "default": DEFAULT_TIMEOUT},
            },
            "required": ["command"],
        },
        execute_fn=_run_bash,
    )
