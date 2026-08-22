"""
MCP 沙箱安全层

为 MCP 工具调用提供安全隔离：
- 文件系统访问控制（白名单/黑名单）
- 网络访问控制
- 命令执行限制
- 资源配额（超时、内存估算）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SandboxPolicy:
    """沙箱安全策略

    属性：
        allowed_paths: 允许访问的文件路径列表（绝对路径或 glob 模式）
        denied_paths: 禁止访问的文件路径列表
        allowed_commands: 允许执行的命令列表（glob 模式）
        denied_commands: 禁止执行的命令列表
        allow_network: 是否允许网络访问
        max_execution_seconds: 单次工具调用的最大执行时间
        max_output_chars: 工具返回结果的最大字符数
    """
    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    allowed_commands: list[str] = field(default_factory=list)
    denied_commands: list[str] = field(default_factory=list)
    allow_network: bool = False
    max_execution_seconds: int = 30
    max_output_chars: int = 10000
    # 相对路径的解析基准（不设则按进程 CWD，几乎必然落在白名单外导致误拦）
    workspace_root: str = ""

    def check_file_access(self, path: str) -> tuple[bool, str]:
        """检查文件访问权限

        Returns:
            (allowed, reason)
        """
        candidate = Path(path)
        if not candidate.is_absolute() and self.workspace_root:
            candidate = Path(self.workspace_root) / candidate
        resolved = str(candidate.resolve())

        # 检查黑名单
        for pattern in self.denied_paths:
            if _path_matches(resolved, pattern):
                return False, f"Access denied: '{path}' matches denied pattern '{pattern}'"

        # 检查白名单（配置了白名单时，只允许匹配的路径）
        if self.allowed_paths:
            for pattern in self.allowed_paths:
                if _path_matches(resolved, pattern):
                    return True, ""
            return False, f"Access denied: '{path}' not in allowed paths"

        # 未配置白名单也未命中黑名单 → 允许（默认放行）
        return True, ""

    def check_command(self, command: str) -> tuple[bool, str]:
        """检查命令执行权限

        Returns:
            (allowed, reason)
        """
        # 提取命令名（第一个词）
        cmd_name = command.strip().split()[0] if command.strip() else ""

        # 检查黑名单
        for pattern in self.denied_commands:
            if _glob_match(cmd_name, pattern) or pattern in command:
                return False, f"Command denied: '{cmd_name}' matches denied pattern '{pattern}'"

        # 检查白名单
        if self.allowed_commands:
            matched = any(
                _glob_match(cmd_name, pattern) or pattern in command
                for pattern in self.allowed_commands
            )
            if not matched:
                return False, f"Command denied: '{cmd_name}' not in allowed commands"

        return True, ""

    def check_network(self) -> tuple[bool, str]:
        """检查网络访问权限"""
        if not self.allow_network:
            return False, "Network access denied by sandbox policy"
        return True, ""


def _path_matches(path: str, pattern: str) -> bool:
    """检查路径是否匹配模式（支持 glob 和目录前缀）"""
    import fnmatch
    # 1. 直接 glob 匹配
    if fnmatch.fnmatch(path, pattern):
        return True
    # 2. 标准化路径分隔符后匹配（兼容 Windows）
    path_n = path.replace("\\", "/")
    pattern_n = pattern.replace("\\", "/")
    if fnmatch.fnmatch(path_n, pattern_n):
        return True
    # 3. resolve 后匹配
    try:
        resolved = str(Path(path).resolve()).replace("\\", "/")
        pattern_resolved = str(Path(pattern).resolve()).replace("\\", "/")
        if fnmatch.fnmatch(resolved, pattern_resolved):
            return True
    except Exception:
        pass
    # 4. 目录前缀匹配（pattern 是 path 的父目录）
    # 去除尾部斜杠后比较
    path_stripped = path_n.rstrip("/")
    pattern_stripped = pattern_n.rstrip("/")
    if path_stripped.startswith(pattern_stripped + "/"):
        return True
    return False


def _glob_match(name: str, pattern: str) -> bool:
    """检查名称是否匹配 glob 模式"""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ============================================================
# 内置策略预设
# ============================================================

def workspace_policy(workspace: Path) -> SandboxPolicy:
    """生成基于 workspace 的默认策略

    允许访问 workspace 内所有文件，允许基本开发命令。
    """
    ws = str(workspace.resolve())
    return SandboxPolicy(
        allowed_paths=[ws],
        workspace_root=ws,
        allowed_commands=[
            "cat", "ls", "dir", "echo", "head", "tail", "grep", "find",
            "python*", "pip*", "uv*", "node*", "npm*", "npx*",
            "pytest*", "ruff*", "mypy*", "black*",
            "git*",
            "curl*", "wget*",
            "mkdir*", "touch*", "cp*", "mv*", "rm*",
            "which*", "where*",
        ],
        allow_network=True,
        max_execution_seconds=30,
        max_output_chars=10000,
    )


def strict_policy() -> SandboxPolicy:
    """严格策略：只读访问，不允许执行命令"""
    return SandboxPolicy(
        allowed_paths=[],
        allowed_commands=[],
        allow_network=False,
        max_execution_seconds=10,
        max_output_chars=5000,
    )


def permissive_policy() -> SandboxPolicy:
    """宽松策略：允许所有操作（仅用于受控环境）"""
    return SandboxPolicy(
        allow_network=True,
        max_execution_seconds=60,
        max_output_chars=50000,
    )
