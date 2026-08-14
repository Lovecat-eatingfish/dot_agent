from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


VALID_APPROVAL_MODES = {"inline", "auto", "deny"}


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    command: str
    risk_reason: str
    tool_name: str = "BashTool"
    # 通用工具审批（approve 模式）可携带序列化参数摘要
    tool_args_preview: str = ""


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


RISK_PATTERNS = [
    (r"(?:^|&&|\|\||;)\s*(?:python\s+-m\s+)?pip\s+install\b", "Python package installation"),
    (r"(?:^|&&|\|\||;)\s*uv\s+add\b", "Project dependency change with uv add"),
    (r"(?:^|&&|\|\||;)\s*uv\s+sync\b", "Dependency synchronization with uv sync"),
    (r"(?:^|&&|\|\||;)\s*uv\s+pip\s+install\b", "Python package installation with uv pip"),
    (r"(?:^|&&|\|\||;)\s*npm\s+install\b", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*pnpm\s+install\b", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*yarn\s+(?:install\b|add\b)", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*(?:curl|wget)\b", "Network download command"),
    (r"(?:^|&&|\|\||;)\s*uvicorn\b", "Long-running development server"),
    (r"(?:^|&&|\|\||;)\s*python\s+-m\s+http\.server\b", "Long-running development server"),
    (r"(?:^|&&|\|\||;)\s*git\s+push\b", "Git push to remote"),
    (r"(?:^|&&|\|\||;)\s*git\s+reset\s+--hard\b", "Git hard reset (destructive)"),
    (r"(?:^|&&|\|\||;)\s*docker\s+(?:run|exec)\b", "Docker container execution"),
    (r"(?:^|&&|\|\||;)\s*ssh\b", "Remote shell access"),
    (r"(?:^|&&|\|\||;)\s*scp\b", "Remote file copy"),
    (r"(?:^|&&|\|\||;)\s*rsync\b", "Remote file sync"),
]


def normalize_approval_mode(mode: str | None) -> str:
    normalized = (mode or "inline").strip().lower()
    return normalized if normalized in VALID_APPROVAL_MODES else "inline"


def classify_command_risk(command: str) -> str | None:
    for pattern, reason in RISK_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return reason
    return None


def make_approval_request(
    command: str,
    risk_reason: str,
    *,
    tool_name: str = "BashTool",
    tool_args_preview: str = "",
) -> ApprovalRequest:
    return ApprovalRequest(
        id=f"approval-{uuid4().hex[:8]}",
        command=command,
        risk_reason=risk_reason,
        tool_name=tool_name,
        tool_args_preview=tool_args_preview,
    )


def resolve_approval(
    handler: Any,
    request: ApprovalRequest,
    *,
    approval_mode: str = "inline",
) -> ApprovalDecision:
    """统一解析审批结果"""
    mode = normalize_approval_mode(approval_mode)
    if mode == "auto":
        return ApprovalDecision(approved=True, reason="approval_mode=auto")
    if mode == "deny":
        return ApprovalDecision(approved=False, reason="approval_mode=deny")
    if handler is None:
        return ApprovalDecision(approved=False, reason="no approval handler")
    decision = handler(request)
    if isinstance(decision, ApprovalDecision):
        return decision
    return ApprovalDecision(approved=bool(decision), reason="handler")
