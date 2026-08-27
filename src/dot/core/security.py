"""
安全策略 — 命令行危险模式检测

定义 Bash 危险命令的黑名单正则，供权限系统和工具层复用。
不在工具实现里定义，避免权限层依赖工具实现细节。
"""
from __future__ import annotations

import re
from typing import Pattern

# 危险命令正则（re.IGNORECASE 匹配）
DANGEROUS_PATTERNS: list[Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        # Unix 递归强制删除（多种标志组合）
        r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b",
        r"\brm\b(?=[^|;&]*\s-[a-zA-Z]*r)(?=[^|;&]*\s-[a-zA-Z]*f)",
        r"\brm\b(?=[^|;&]*--recursive)(?=[^|;&]*--force)",
        # PowerShell 递归删除
        r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
        # Windows 静默删除
        r"\b(?:del|rmdir)\s+/[sq]\b",
        # 格式化磁盘
        r"\bformat\s+(?:[a-zA-Z]:|/q)",
        # 系统控制
        r"\bshutdown\b",
        r"\breboot\b",
        # 重定向到非 /dev/null
        r"(?:^|[^0-9])>\s*(?:[A-Za-z]:\\|/(?!dev/null\b))",
        r"\bmkfs\b",
        r"\bdd\s+",
        # 权限修改
        r"\bchmod\s+777\b",
        r"\bchown\b",
        # 进程控制
        r"\bkill\s+-9\b",
        r"\bpkill\b",
        r"\biptables\b",
        # shell 注入
        r"(?:^|[;&|])\s*eval\s",
        r"(?:^|[;&|])\s*exec\s",
        # pipe 到任意 shell
        r"\|\s*(?:ba|z|da|k)?sh\b",
    ]
]


def looks_dangerous(command: str) -> str | None:
    """检测命令是否命中危险模式，返回匹配的原文，未命中返回 None。"""
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return pattern.pattern
    return None
