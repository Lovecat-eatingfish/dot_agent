"""
Agent 运行模式（对齐 Claude Code / docs/agent.md）

模式：
- auto:    全自动，工具放行；仍拦截明显毁灭性命令（rm -rf / 等）
- plan:    只规划，写出 id_todo.md，不执行 mutating 工具，等待确认
- approve: 每个高危工具（edit/write/bash/MCP 写）都需人工审批
- edit:    只允许文件读写类工具，禁用 bash、网络 MCP 等
- bypass:  跳过审批（对齐 Claude Code bypassPermissions），但毁灭性命令仍硬拦

与 approval_mode（inline/auto/deny）正交：
- agent_mode 决定「哪些工具允许 / 是否进入 plan」
- approval_mode 决定「高危 bash 如何确认」
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

VALID_AGENT_MODES = {"auto", "plan", "approve", "edit", "bypass"}

# edit 模式允许的工具（只读探索 + 文件读写，禁 bash / 网络 / MCP 写）
# edit 模式定义是「只允许文件读写类工具，禁用 bash、网络 MCP 等」。
# MemoryWriteTool 属记忆层落盘而非代码文件读写，从 edit 集移除（review #17）。
_EDIT_MODE_ALLOW = frozenset({
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GlobTool",
    "GrepTool",
    "NotepadReadTool",
    "NotepadAppendTool",
    "SkillTool",
    "TodoUpdateTool",
    "MemoryIndexTool",
    "MemoryReadTool",
})

# plan 模式允许的只读探索工具
_PLAN_MODE_ALLOW = frozenset({
    "FileReadTool",
    "GlobTool",
    "GrepTool",
    "NotepadReadTool",
    "WebSearchTool",
    "SkillTool",
    "TodoUpdateTool",
    "MemoryIndexTool",
    "MemoryReadTool",
})

# 视为 mutating / 高危（approve 模式需要审批）
_MUTATING_TOOLS = frozenset({
    "FileWriteTool",
    "FileEditTool",
    "BashTool",
    "NotepadAppendTool",
    "MemoryWriteTool",
    "AgentTool",  # 派生子 Agent 可间接写盘，approve 模式需确认
})

# auto 模式仍硬拦的毁灭性命令
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/", re.I),
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bdel\s+/[sf]\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\s*\(\s*\)\s*\{", re.I),  # fork bomb
]


@dataclass(frozen=True)
class ModeGateResult:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False


def normalize_agent_mode(mode: str | None) -> str:
    normalized = (mode or "auto").strip().lower()
    return normalized if normalized in VALID_AGENT_MODES else "auto"


def is_mutating_tool(tool_name: str) -> bool:
    if tool_name in _MUTATING_TOOLS:
        return True
    # MCP 工具默认视为可能有副作用
    if tool_name.startswith("mcp__"):
        return True
    return False


def is_destructive_bash(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _DESTRUCTIVE_PATTERNS)


def check_tool_permission(
    agent_mode: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> ModeGateResult:
    """按 agent_mode 判定工具是否可执行"""
    mode = normalize_agent_mode(agent_mode)
    args = tool_args or {}

    if mode == "edit":
        if tool_name not in _EDIT_MODE_ALLOW:
            return ModeGateResult(
                allowed=False,
                reason=f"agent_mode=edit blocks '{tool_name}' (file tools only)",
            )
        return ModeGateResult(allowed=True)

    if mode == "plan":
        if tool_name not in _PLAN_MODE_ALLOW:
            return ModeGateResult(
                allowed=False,
                reason=(
                    f"agent_mode=plan blocks '{tool_name}'. "
                    "Write the plan to id_todo.md and wait for user confirmation."
                ),
            )
        return ModeGateResult(allowed=True)

    if mode == "approve":
        if is_mutating_tool(tool_name):
            return ModeGateResult(
                allowed=True,
                needs_approval=True,
                reason=f"agent_mode=approve requires confirmation for '{tool_name}'",
            )
        return ModeGateResult(allowed=True)

    if mode == "bypass":
        # bypassPermissions：跳过审批，但毁灭性命令仍硬拦
        if tool_name == "BashTool" and is_destructive_bash(str(args.get("command", ""))):
            return ModeGateResult(
                allowed=False,
                reason="destructive bash command blocked even in bypass mode",
            )
        return ModeGateResult(allowed=True)

    # auto
    if tool_name == "BashTool" and is_destructive_bash(str(args.get("command", ""))):
        return ModeGateResult(
            allowed=False,
            reason="destructive bash command blocked in auto mode",
        )
    return ModeGateResult(allowed=True)
