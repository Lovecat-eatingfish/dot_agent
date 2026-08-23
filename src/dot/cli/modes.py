"""
运行模式定义（对齐设计文档 §3）

三种工作模式，支持 Tab/Shift+Tab 循环切换、/mode 命令切换，热生效。
模式存入 Session.run_mode，下一轮对话立即生效，不侵入 Agent 核心节点。

- agent：完整模式（默认），全量工具：规划/反思/文件读写/Shell/MCP/检索
- chat：纯对话模式，禁用所有工具，仅 LLM 问答
- code：代码专注模式，仅安全文件工具（read/write/glob/grep），禁用 Shell/MCP
"""
from __future__ import annotations

from typing import Literal

RunMode = Literal["agent", "chat", "code"]

# 正向循环顺序（Tab）
RUN_MODES: tuple[str, ...] = ("agent", "chat", "code")

_MODE_LABELS: dict[str, str] = {
    "agent": "Agent",
    "chat": "Chat",
    "code": "Code",
}

_MODE_DESC: dict[str, str] = {
    "agent": "完整模式：规划/反思/文件读写/Shell/MCP 全量开放",
    "chat": "纯对话模式：禁用所有工具，仅 LLM 问答",
    "code": "代码专注模式：仅文件读写/检索，禁用 Shell/MCP",
}


def is_valid_mode(mode: str) -> bool:
    return mode in RUN_MODES


def normalize_mode(mode: str) -> str:
    """归一化模式名（容错：大小写/简写），无效则返回 agent"""
    m = (mode or "").strip().lower()
    if m in RUN_MODES:
        return m
    # 容错简写
    aliases = {"a": "agent", "c": "chat", "co": "code", "full": "agent"}
    return aliases.get(m, "agent")


def cycle_mode(current: str, *, forward: bool = True) -> str:
    """循环切换模式：forward=True agent→chat→code，反向 code→chat→agent"""
    cur = current if current in RUN_MODES else RUN_MODES[0]
    idx = RUN_MODES.index(cur)
    step = 1 if forward else -1
    return RUN_MODES[(idx + step) % len(RUN_MODES)]


def mode_label(mode: str) -> str:
    return _MODE_LABELS.get(mode, mode)


def mode_desc(mode: str) -> str:
    return _MODE_DESC.get(mode, "")
