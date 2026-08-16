"""
工具执行前统一门禁：agent_mode + approve 审批 + Hook 之前的硬规则
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any

from mokioclaw.security.agent_mode import ModeGateResult, check_tool_permission
from mokioclaw.security.approval import classify_command_risk
from mokioclaw.security.approval import ApprovalRequest, make_approval_request, resolve_approval
from mokioclaw.state.runtime import RuntimeState


def gate_tool_call(
    runtime: RuntimeState | None,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any] | None:
    """若应阻断，返回错误结果字典；否则返回 None 表示放行。

    approve 模式下对 mutating 工具走 approval_handler。
    """
    if runtime is None:
        return None

    explicit_block = _check_explicit_tool_rules(runtime, tool_name)
    if explicit_block is not None:
        return explicit_block

    gate: ModeGateResult = check_tool_permission(
        getattr(runtime, "agent_mode", "auto"),
        tool_name,
        tool_args,
    )
    if not gate.allowed:
        reason = gate.reason or "blocked by agent_mode"
        return _permission_error(tool_name, reason)

    if gate.needs_approval:
        preview = json.dumps(tool_args, ensure_ascii=False, default=str)[:500]
        request = make_approval_request(
            command=preview if tool_name != "BashTool" else str(tool_args.get("command", "")),
            risk_reason=gate.reason or f"{tool_name} requires approval",
            tool_name=tool_name,
            tool_args_preview=preview,
        )
        decision = resolve_approval(
            runtime.approval_handler,
            request,
            # approve 模式下强制走 handler；若 handler 缺失则 deny
            approval_mode="inline" if runtime.approval_handler else "deny",
        )
        if not decision.approved:
            reason = f"user denied {tool_name}: {decision.reason or gate.reason}"
            return {
                "ok": False,
                "error": reason,
                "requires_approval": True,
                "approved": False,
                "approval_id": request.id,
                "risk_reason": request.risk_reason,
                "approval_preview": _approval_preview(tool_name, tool_args, gate.reason),
                "recoverable": True,
                "suggested_fix": _suggest_permission_fix(tool_name, reason),
            }
    return None



def _check_explicit_tool_rules(runtime: RuntimeState, tool_name: str) -> dict[str, Any] | None:
    disallowed = list(getattr(runtime, "disallowed_tools", []) or [])
    allowed = list(getattr(runtime, "allowed_tools", []) or [])

    matched_deny = _first_tool_rule_match(tool_name, disallowed)
    if matched_deny:
        reason = f"tool '{tool_name}' blocked by disallowed_tools rule '{matched_deny}'"
        return _permission_error(tool_name, reason, permission_rule=matched_deny)

    if allowed and not _first_tool_rule_match(tool_name, allowed):
        reason = f"tool '{tool_name}' is not listed in allowed_tools"
        return _permission_error(tool_name, reason, permission_rule="allowed_tools")

    return None


def _first_tool_rule_match(tool_name: str, rules: list[str]) -> str | None:
    for raw_rule in rules:
        rule = str(raw_rule).strip()
        if not rule:
            continue
        if rule == tool_name or fnmatch.fnmatchcase(tool_name, rule):
            return rule
    return None


def _permission_error(tool_name: str, reason: str, *, permission_rule: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "error": reason,
        "recoverable": True,
        "suggested_fix": _suggest_permission_fix(tool_name, reason),
    }
    if permission_rule:
        result["permission_rule"] = permission_rule
    return result

def _approval_preview(tool_name: str, tool_args: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    command = str(tool_args.get("command", "")) if tool_name == "BashTool" else ""
    return {
        "tool": tool_name,
        "action": _tool_action_summary(tool_name, tool_args),
        "risk_reason": reason or (classify_command_risk(command) if command else "mutating tool requires approval"),
        "args_preview": json.dumps(tool_args, ensure_ascii=False, default=str)[:500],
    }


def _tool_action_summary(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name == "BashTool":
        return f"Run shell command: {tool_args.get('command', '')}"
    if tool_name == "FileWriteTool":
        return f"Write file: {tool_args.get('file_path') or tool_args.get('path') or '(unknown path)'}"
    if tool_name == "FileEditTool":
        return f"Edit file: {tool_args.get('file_path') or tool_args.get('path') or '(unknown path)'}"
    if tool_name.startswith("mcp__"):
        return f"Call MCP tool: {tool_name}"
    return f"Call tool: {tool_name}"


def _suggest_permission_fix(tool_name: str, reason: str) -> str:
    lowered = reason.lower()
    if "plan" in lowered:
        return "Switch to auto or approve mode after reviewing the generated plan."
    if "edit" in lowered:
        return "Use file tools only in edit mode, or switch modes before running shell/network tools."
    if "denied" in lowered:
        return "Ask the user to approve the action or choose a safer alternative."
    if "destructive" in lowered:
        return "Avoid the destructive command and make a narrower, reversible change."
    return f"Request permission for {tool_name} or choose an allowed alternative."
