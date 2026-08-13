"""
工具执行前统一门禁：agent_mode + approve 审批 + Hook 之前的硬规则
"""
from __future__ import annotations

import json
from typing import Any

from mokioclaw.security.agent_mode import ModeGateResult, check_tool_permission
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

    gate: ModeGateResult = check_tool_permission(
        getattr(runtime, "agent_mode", "auto"),
        tool_name,
        tool_args,
    )
    if not gate.allowed:
        return {"ok": False, "error": gate.reason or "blocked by agent_mode"}

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
            return {
                "ok": False,
                "error": f"user denied {tool_name}: {decision.reason or gate.reason}",
                "requires_approval": True,
            }
    return None
