"""
Auto-mode 工具安全分类器（对齐 Claude Code yoloClassifier）

两段式判定：
1. 快速规则过滤：只读命令 / 已知安全模式 → 直接放行；毁灭性命令 → 直接拒绝
2. LLM 评估：规则未覆盖时，用轻量模型按对话上下文评估 allow / deny / ask

默认关闭：需 MOKIO_AUTO_CLASSIFIER=1 显式开启（对齐 Claude Code 的
TRANSCRIPT_CLASSIFIER feature flag）。关闭时 approval_mode=auto 退化为
旧的「自动批准」行为。
"""
from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class ClassifierDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # 置信度不足，回退人工


# 只读 / 无副作用命令 fast-path（直接放行，不进分类器）
_SAFE_COMMAND_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"^\s*(ls|dir|pwd|cd|echo|type|cat|head|tail|find|where|which)\b",
        r"^\s*(git\s+(status|diff|log|show|branch|rev-parse|ls-files|blame))\b",
        r"^\s*(python|python3|node|npm|uv|pip)\s+(--version|-V|--help)\b",
        r"^\s*(python|python3)\s+-c\s+[\"']?\s*(import|print)\b",
        r"^\s*(pytest|python\s+-m\s+pytest|ruff|mypy|black|eslint|tsc)\b",
        r"^\s*(grep|rg|findstr|select-string)\b",
    )
]

# 明显危险：分类器不再询问，直接 deny
# 注意：handoff fast-path 用同一组模式扫描「子 Agent 行为摘要」，所以 rm -rf 模式
# 必须只匹配毁灭性目标（系统路径 / 用户家目录），不能误伤 rm -rf node_modules 这类
# 工作区内常见清理操作。
_HARD_DENY_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/",            # rm -rf / (根目录)
        r"\brm\s+-rf\b\s+(~|\$HOME|\$PWD|/(?:home|usr|etc|var|root|bin|boot|sbin|lib))",  # rm -rf 系统路径
        r"\brm\s+-rf\b\s+\.\s*$",                        # rm -rf .（当前目录根）
        r"\bformat\s+[a-z]:",
        r"\bmkfs\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
        r"\bdd\s+if=",
        r":\s*\(\s*\)\s*\{",  # fork bomb
        r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh\b",  # 下载即执行
    )
]

_CLASSIFIER_PROMPT = """You are a security classifier for a coding agent's tool calls.

Decide whether the proposed tool call is safe to run WITHOUT asking the user.

Rules:
- Read-only, reversible, workspace-local actions → allow
- Installs, network downloads, long-running servers, git push, writes outside
  the workspace, or anything hard to reverse → deny
- If uncertain → ask

Respond with JSON only: {{"decision": "allow|deny|ask", "reason": "<short>"}}

Tool: {tool_name}
Arguments: {tool_args}
Recent context: {context}
"""


def classifier_enabled() -> bool:
    return os.getenv("MOKIO_AUTO_CLASSIFIER", "").strip().lower() in {"1", "true", "yes"}


def fast_path_decision(tool_name: str, tool_args: dict[str, Any]) -> ClassifierDecision | None:
    """规则快速通道；返回 None 表示需要 LLM 评估"""
    if tool_name != "BashTool":
        return None
    command = str(tool_args.get("command", "")).strip()
    if not command:
        return ClassifierDecision.DENY
    if any(p.search(command) for p in _HARD_DENY_PATTERNS):
        return ClassifierDecision.DENY
    if any(p.search(command) for p in _SAFE_COMMAND_PATTERNS):
        return ClassifierDecision.ALLOW
    return None


def classify_tool_call(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    context: str = "",
) -> tuple[ClassifierDecision, str]:
    """两段式分类：fast-path → LLM。LLM 失败时保守返回 ASK。"""
    fast = fast_path_decision(tool_name, tool_args)
    if fast is not None:
        return fast, "fast-path rule"

    if not classifier_enabled():
        # 未开启 LLM 分类器：未知命令交给人工
        return ClassifierDecision.ASK, "classifier disabled"

    try:
        from mokioclaw.providers.openai_provider import create_model

        prompt = _CLASSIFIER_PROMPT.format(
            tool_name=tool_name,
            tool_args=json.dumps(tool_args, ensure_ascii=False, default=str)[:2000],
            context=context[-2000:],
        )
        response = create_model().invoke(prompt)
        text = str(getattr(response, "content", response) or "")
        parsed = _extract_json(text) or {}
        decision_raw = str(parsed.get("decision", "")).strip().lower()
        reason = str(parsed.get("reason") or "llm classifier")
        if decision_raw == "allow":
            return ClassifierDecision.ALLOW, reason
        if decision_raw == "deny":
            return ClassifierDecision.DENY, reason
        return ClassifierDecision.ASK, reason
    except Exception as exc:
        logger.warning("auto classifier failed, falling back to ask: %s", exc)
        return ClassifierDecision.ASK, f"classifier error: {type(exc).__name__}"


def classify_handoff(
    task: str,
    summary: str,
    *,
    context: str = "",
) -> tuple[ClassifierDecision, str]:
    """Handoff 分类器：在把子 Agent 结果交回父级前审查其行为摘要

    对齐 Claude Code classifyHandoff —— 防止子 Agent 在委派执行期间做了
    危险/越界操作而父级无感知。返回 (decision, reason)：
    - ALLOW: 摘要无异常，正常交接
    - DENY: 摘要含明确危险操作（rm -rf / 写盘越界 / 下载即执行等）
    - ASK: 置信度不足，建议人工复核

    两段式：fast-path 规则扫描 summary + task 文本 → LLM 评估。
    默认关闭（需 MOKIO_AUTO_CLASSIFIER=1）；关闭时返回 ALLOW（不阻断交接）。
    """
    text = f"{task}\n{summary}"

    # fast-path：硬拒绝模式扫描摘要文本
    if any(p.search(text) for p in _HARD_DENY_PATTERNS):
        return ClassifierDecision.DENY, "handoff summary matches hard-deny pattern"

    if not classifier_enabled():
        return ClassifierDecision.ALLOW, "handoff classifier disabled"

    try:
        from mokioclaw.providers.openai_provider import create_model

        prompt = _HANDOFF_PROMPT.format(
            task=task[:1000],
            summary=summary[:2000],
            context=context[-1000:],
        )
        response = create_model().invoke(prompt)
        text_out = str(getattr(response, "content", response) or "")
        parsed = _extract_json(text_out) or {}
        decision_raw = str(parsed.get("decision", "")).strip().lower()
        reason = str(parsed.get("reason") or "llm handoff classifier")
        if decision_raw == "deny":
            return ClassifierDecision.DENY, reason
        if decision_raw == "ask":
            return ClassifierDecision.ASK, reason
        return ClassifierDecision.ALLOW, reason
    except Exception as exc:
        logger.warning("handoff classifier failed, fail-open to allow: %s", exc)
        return ClassifierDecision.ALLOW, f"handoff classifier error: {type(exc).__name__}"


_HANDOFF_PROMPT = """You are reviewing a sub-agent's work summary before handing its result back to the parent agent.

The parent delegated a task to a sub-agent. Now inspect the sub-agent's summary for
dangerous or out-of-scope actions it may have taken during delegation:
- destructive shell commands (rm -rf, format, mkfs, shutdown)
- writes outside the workspace / system paths
- downloading and executing remote scripts
- exfiltrating data to external services

Decide whether the handoff result is safe to accept.

Rules:
- No dangerous actions detected → allow
- Clear dangerous / out-of-scope action detected → deny
- Uncertain → ask

Respond with JSON only: {{"decision": "allow|deny|ask", "reason": "<short>"}}

Delegated task: {task}
Sub-agent summary: {summary}
Context: {context}
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个 JSON 对象。

    对齐 Claude Code 的宽容解析：优先匹配可成功 json.loads 的对象。
    旧正则 `\\{[^{}]*\\}` 只能匹配最内层不含花括号的块，当 reason 字段
    含 `{path}` 等花括号时会误匹配到内层片段导致解析失败（review #16）。
    """
    # 尝试匹配平衡花括号的 JSON 对象（从每个 '{' 起找配对的 '}'）
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        data = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # 从这个 '{' 起无法成对，换下一个
                    return data if isinstance(data, dict) else None
    return None
