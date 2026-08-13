"""
Shell 沙箱（对齐 Claude Code shouldUseSandbox）

与权限系统正交：权限决定「能不能跑」，沙箱决定「在什么隔离边界里跑」。
当前实现为轻量级 workspace 约束（无容器），提供两条防线：

1. 命令静态检查：尝试把工作区外的绝对路径写入 / 切出工作区的命令直接拒绝
2. 环境隔离：可选地剥离网络相关环境变量代理（MOKIO_SANDBOX_NO_NETWORK=1）

真正的 OS 级隔离（容器 / seatbelt）留作后续；配置开关：
- MOKIO_SANDBOX=1（默认开）/ 0（关）
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from mokioclaw.core.log import get_logger

if TYPE_CHECKING:
    from mokioclaw.state.runtime import RuntimeState

logger = get_logger(__name__)

# 沙箱豁免：整条命令是纯只读命令（不含重定向/管道/绝对路径）时跳过路径扫描
_SANDBOX_EXEMPT = re.compile(
    r"^\s*(ls|dir|pwd|cd|echo\s+[^/\\\s][^|&;<>]*|type\s+[^/\\\s][^|&;<>]*|"
    r"cat\s+[^/\\\s][^|&;<>]*|head\s+[^/\\\s][^|&;<>]*|tail\s+[^/\\\s][^|&;<>]*|"
    r"git\s+(status|diff|log|show)|python\s+--version)\s*$",
    re.I,
)

# 命令中引用绝对路径的模式（Windows 盘符 / POSIX 根路径）
_ABS_PATH_PATTERN = re.compile(r"(?<![\w/.-])(?:[A-Za-z]:[\\/][^\s\"'|&;<>]*|/(?!/)[^\s\"'|&;<>]+)")


def sandbox_enabled() -> bool:
    return os.getenv("MOKIO_SANDBOX", "1").strip().lower() not in {"0", "false", "no", "off"}


def check_sandbox(state: "RuntimeState", command: str) -> str | None:
    """返回拒绝原因；None 表示放行。

    规则：命令里出现的绝对路径必须落在 workspace 内（/dev/null、临时管道等除外）。
    """
    if not sandbox_enabled():
        return None
    text = (command or "").strip()
    if not text or _SANDBOX_EXEMPT.match(text):
        return None

    workspace = state.workspace.resolve()
    for match in _ABS_PATH_PATTERN.finditer(text):
        raw = match.group(0)
        # 常见无害目标
        if raw.startswith(("/dev/", "/tmp/", "/proc/", "/sys/")):
            continue
        if raw.lower().startswith(("nul", "con")):
            continue
        candidate = Path(raw)
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        # Windows 上 POSIX 风格路径会被解析到当前盘符，需以 workspace 盘符为基准判断
        if os.name == "nt":
            if candidate.drive and candidate.drive.lower() != workspace.drive.lower():
                return f"sandbox: absolute path outside workspace drive: {raw}"
            if not candidate.drive and raw.startswith("/"):
                return f"sandbox: POSIX absolute path not allowed on Windows: {raw}"
            # 用 is_relative_to 做路径边界判断，避免 startswith 的前缀误判
            # （workspace=D:/a/b 会误放行 D:/a/b-c，review #18）
            try:
                in_workspace = resolved.is_relative_to(workspace)
            except ValueError:
                in_workspace = False
            # is_relative_to 是 3.9+ 才有，但项目要求 >=3.13，直接用
        else:
            try:
                in_workspace = resolved.is_relative_to(workspace)
            except ValueError:
                in_workspace = False
        if not in_workspace:
            return f"sandbox: absolute path outside workspace is not allowed: {raw}"
    return None


def sandbox_env_overrides(env: dict[str, str]) -> dict[str, str]:
    """按 MOKIO_SANDBOX_NO_NETWORK=1 剥离代理变量（best-effort 网络约束）"""
    if os.getenv("MOKIO_SANDBOX_NO_NETWORK", "").strip().lower() in {"1", "true", "yes"}:
        for key in list(env):
            if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
                env.pop(key, None)
        env["MOKIO_SANDBOXED"] = "1"
    return env
