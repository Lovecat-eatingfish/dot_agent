"""
思考模式关键词解析

对齐 Claude Code：
- think          基础思考
- think hard     深入思考
- think harder   更深入思考
- ultrathink     最深度思考

从用户输入开头剥离关键词，返回净化后的任务文本 + 注入 system 动态区的指令。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_THINKING_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"^\s*ultrathink\b[\s,:：-]*", re.IGNORECASE),
        "ultrathink",
        (
            "Thinking mode: ULTRATHINK. Spend maximal reasoning effort before acting. "
            "Explore alternatives, surface risks, verify assumptions, then execute carefully."
        ),
    ),
    (
        re.compile(r"^\s*think\s+harder\b[\s,:：-]*", re.IGNORECASE),
        "think harder",
        (
            "Thinking mode: THINK HARDER. Reason thoroughly about edge cases, "
            "dependencies, and failure modes before making changes."
        ),
    ),
    (
        re.compile(r"^\s*think\s+hard\b[\s,:：-]*", re.IGNORECASE),
        "think hard",
        (
            "Thinking mode: THINK HARD. Take extra time to plan, consider trade-offs, "
            "and verify results after edits."
        ),
    ),
    (
        re.compile(r"^\s*think\b[\s,:：-]*", re.IGNORECASE),
        "think",
        (
            "Thinking mode: THINK. Prefer deliberate step-by-step reasoning "
            "before tool use; avoid rushing to edits."
        ),
    ),
]


@dataclass(frozen=True)
class ThinkingMode:
    keyword: str
    instruction: str
    cleaned_text: str


def parse_thinking_mode(text: str) -> ThinkingMode | None:
    """解析用户输入中的思考模式关键词

    Returns:
        ThinkingMode 或 None（未匹配）
    """
    if not text:
        return None
    for pattern, keyword, instruction in _THINKING_PATTERNS:
        match = pattern.match(text)
        if match:
            cleaned = text[match.end():].lstrip()
            return ThinkingMode(
                keyword=keyword,
                instruction=instruction,
                cleaned_text=cleaned or text.strip(),
            )
    return None


def apply_thinking_mode(text: str) -> tuple[str, str]:
    """返回 (cleaned_task, thinking_instruction)

    未匹配时 instruction 为空字符串。
    """
    parsed = parse_thinking_mode(text)
    if parsed is None:
        return text, ""
    return parsed.cleaned_text, parsed.instruction
