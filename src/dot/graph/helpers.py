"""
辅助函数 — 从 coding_graph.py 拆分

职责：
  - LLM 调用入口（_invoke_llm）
  - 工具执行（_run_tool_call）
  - 计划质量评估（_evaluate_plan_quality）
  - 校验结果解析（_parse_validation_result, _extract_json_text）
  - 静态上下文加载（_load_static_context, _load_file_safe）
  - Host 安全调用（_safe_host_call）
  - 最终答复生成（_last_final_answer）
  - 日志辅助（_short_args）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ..core.log import get_logger
from ..core.utils import last_ai_content
from ..tools.meta import dispatch_special_tool

if TYPE_CHECKING:
    from ..session.session import Session
    from ..session.agent_context import AgentContext

logger = get_logger(__name__)


def invoke_llm(messages: list[Any], *, node: str, tools: list[Any] | None = None, config: Any = None) -> Any:
    """统一的 LLM 调用入口（带链路追踪 span）"""
    import os

    from ..core.llm import create_model
    from ..trace import get_tracer

    span = get_tracer().start_span(
        "llm", "llm_inference",
        tags={
            "node": node,
            "model": os.environ.get("MODEL", ""),
            "messages": len(messages),
            "with_tools": bool(tools),
        },
    )
    try:
        model = create_model()
        if tools:
            model = model.bind_tools(tools)
        response = model.invoke(messages, config=config) if config is not None else model.invoke(messages)
        content = getattr(response, "content", "")
        tool_calls = getattr(response, "tool_calls", None) or []
        _raw = str(content) if content else "(empty)"
        logger.info("[%s] LLM response: content_len=%d, tool_calls=%d, raw=%s",
                    node, len(_raw), len(tool_calls), _raw[:500])
        span.set_output_summary(f"content_len={len(str(content))} tool_calls={len(tool_calls)}")
        span.finish()
        return response
    except BaseException as exc:
        span.set_output_summary(f"{type(exc).__name__}: {exc}")
        span.finish(exc)
        raise


def run_tool_call(
        session: Session,
        ctx: AgentContext,
        tools: list[Any],
        call: dict[str, Any],
        *,
        prefix: str,
) -> ToolMessage | None:
    """执行单个工具调用（对齐 fix.md / fix-权限控制.md）"""
    from ..core.permission import Decision
    from ..core.utils import execute_tool_by_name

    writer = _get_writer()
    tool_name = call.get("name", "")
    args = call.get("args") or {}

    # ---- 权限校验 ----
    pm = ctx.permission_manager
    decision = pm.check(tool_name, args, agent_mode=session.agent_mode)

    if decision.decision is Decision.DENY:
        writer({"type": "permission_denied", "name": tool_name, "source": decision.source, "reason": decision.reason})
        return ToolMessage(
            content=decision.deny_message(),
            tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
        )

    if decision.decision is Decision.ASK:
        approved = pm.ask_user(tool_name, args, decision, agent_mode=session.agent_mode)
        if not approved:
            writer({"type": "permission_denied", "name": tool_name, "source": "user", "reason": "user rejected"})
            return ToolMessage(
                content="用户拒绝了本次操作的审批请求。",
                tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
            )
        decision = pm.check(
            tool_name, args, agent_mode=session.agent_mode, approved=True,
        )
        if decision.decision is Decision.DENY:
            writer(
                {"type": "permission_denied", "name": tool_name, "source": decision.source, "reason": decision.reason})
            return ToolMessage(
                content=decision.deny_message(),
                tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
            )

    # 渐进披露特殊分发
    special = dispatch_special_tool(session, ctx, call)
    if special is not None:
        _sc = getattr(special, "content", "")
        logger.info("[tool:%s] dispatch_special_tool returned: %s", tool_name, _sc[:300])
        return special

    try:
        result_msg = execute_tool_by_name(
            tools=tools,
            call=call,
            session=session,
            ctx=ctx,
        )
        _rc = getattr(result_msg, "content", "") if result_msg else ""
        logger.info("[tool:%s] executed, result: %s", tool_name, _rc[:500])
        return result_msg
    except Exception as exc:
        logger.warning("tool execution failed: %s", exc, exc_info=True)
        return ToolMessage(
            content=f"Error: {exc}",
            tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
        )


def evaluate_plan_quality(
        *,
        plan_description: str,
        subtasks: list[dict],
        tool_call_count: int,
        loop_count: int,
        summary: str,
) -> tuple[bool, str]:
    """LLM驱动评估计划执行质量"""
    if not plan_description and not subtasks:
        if not summary or summary.startswith("Executed"):
            return False, "Plan is empty — no description or subtasks provided."
        logger.info("[evaluate] plan empty but agent produced output, delegating to LLM evaluator")

    if tool_call_count == 0 and summary and len(summary) > 5:
        logger.info("[evaluate] chat-only task (no tools, has text), auto-pass")
        return True, ""

    eval_system = """
You are a plan‑execution evaluator.
Given original plan, execution statistics and agent output, judge whether the task execution is reasonable and finished.

Rules:
1. Return ONLY JSON object, no markdown, no extra text.
2. Output schema:
{
  "is_reasonable": boolean,
  "issue": string
}
- is_reasonable = true: execution matches plan, task is completed or can proceed to validation node.
- is_reasonable = false: execution deviates from plan, stuck, meaningless loop, failed to complete subtasks → need replan.
- issue: when is_reasonable=false, write concise reason; otherwise empty string.

CRITICAL RULES:
- For conversational tasks (greetings, questions, explanations, chat): no tool calls is CORRECT. If the agent produced a meaningful text response, is_reasonable MUST be true.
- For coding tasks: zero effective tool calls + no code output = unreasonable.
- Do NOT penalize the agent for not using tools when the task doesn't require them.
"""

    eval_user_prompt = f"""
=== Original Plan ===
plan_description: {plan_description}
subtasks: {json.dumps(subtasks, ensure_ascii=False)}

=== Execution Statistic ===
loop_count: {loop_count}
tool_call_count: {tool_call_count}

=== Agent Final Output Summary ===
summary: {summary}

Please evaluate this plan execution.
"""
    messages = [
        {"role": "system", "content": eval_system},
        {"role": "user", "content": eval_user_prompt},
    ]

    try:
        resp = invoke_llm(messages, node="plan_evaluate", tools=None)
        raw_content = getattr(resp, "content", "")
        parsed, _ = json.JSONDecoder().raw_decode(raw_content.strip())
        is_reasonable = bool(parsed.get("is_reasonable", True))
        issue = str(parsed.get("issue", ""))
        return is_reasonable, issue

    except Exception as e:
        logger.warning("evaluate_plan_quality llm failed, fallback pass: %s", e, exc_info=True)
        return True, ""


def parse_validation_result(messages: list[Any]) -> dict[str, Any]:
    """从最后的 AI 消息解析校验 JSON"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = extract_json_text(str(getattr(msg, "content", "") or ""))
            if text:
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text)
                    if isinstance(parsed, dict):
                        return {
                            "passed": bool(parsed.get("passed", False)),
                            "error_msg": str(parsed.get("reason", "")),
                            "fail_reason": str(parsed.get("reason", "")),
                            "checks": parsed.get("checks", []),
                        }
                except json.JSONDecodeError:
                    continue
    return {
        "passed": False,
        "error_msg": "Verifier did not return valid JSON.",
        "fail_reason": "Verifier did not return valid JSON.",
        "checks": [],
    }


def extract_json_text(text: str) -> str:
    """剥 markdown 围栏 / think 标签，截取第一个 { 起的 JSON 文本"""
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "```" in text:
        lines = text.splitlines()
        start_idx = None
        end_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith("```") and start_idx is None:
                start_idx = i + 1
            elif line.strip() == "```" and start_idx is not None:
                end_idx = i
                break
        if start_idx is not None and end_idx is not None:
            text = "\n".join(lines[start_idx:end_idx]).strip()
    start = text.find("{")
    if start == -1:
        return ""
    return text[start:]


def load_static_context(session: Session) -> str:
    """加载静态上下文：.dot/memory/dot.md"""
    return load_file_safe(session.workspace / ".dot" / "memory" / "dot.md")


def load_file_safe(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.debug("failed to load %s: %s", path, exc)
    return ""


def safe_host_call(host: Any, method: str, default: str, *args: Any) -> str:
    """安全调用 host 方法，失败返回默认值"""
    if host is None:
        return default
    try:
        result = getattr(host, method)(*args)
        return result if isinstance(result, str) else default
    except Exception as exc:
        logger.debug("host call %s failed: %s", method, exc)
        return default


def last_final_answer(session: Session) -> str:
    """生成最终答复文本"""
    passed = session.turn.validate_result.get("passed", False)
    status = "PASSED" if passed else ("STOPPED" if session.turn.resume_action == "stop" else "FAILED")
    summary = session.turn.task_plan.get("description", "") or session.turn.task
    body = last_ai_content(session.messages[-8:]) if session.messages else ""
    answer = f"[{status}] {summary}"
    if body:
        answer += f"\n\n{body[:2000]}"
    return answer


def short_args(args: Any) -> str:
    """截断工具参数用于日志"""
    if not args:
        return ""
    text = str(args)
    return text[:120] + ("..." if len(text) > 120 else "")


def _get_writer() -> Any:
    """获取 langgraph stream writer；无 stream 上下文时返回 no-op"""
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except RuntimeError:
        return lambda event: None
