"""
图节点实现 — 从 coding_graph.py 拆分

职责：
  - plan_node: 规划节点
  - coding_agent_node: 编码执行节点
  - valid_node: 校验节点
  - human_intervene_node: 人工介入节点
  - finally_node: 终止节点
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from ..core.log import get_logger
from ..core.utils import last_ai_content
from ..tools.meta import build_tools_for_session
from .prompts import get_coding_system_prompt, get_plan_system_prompt, get_valid_system_prompt
from .helpers import (
    evaluate_plan_quality,
    extract_json_text,
    invoke_llm,
    last_final_answer,
    load_static_context,
    parse_validation_result,
    run_tool_call,
    safe_host_call,
    short_args,
    _get_writer,
)

if TYPE_CHECKING:
    from .coding_graph import DotAgentState

logger = get_logger(__name__)

# 工具循环上限
CODING_MAX_LOOPS = 10
VALID_MAX_LOOPS = 8


def plan_node(state: DotAgentState) -> dict[str, Any]:
    """规划节点：读取 messages，生成 task_plan JSON"""
    session = state["session"]
    writer = _get_writer()
    plan = session.get_plan()
    error_feedback = plan.get("error_feedback", "")
    logger.info(
        "[node:plan] entering (replan_count=%d, has_feedback=%s)",
        session.get_replan_count(), bool(error_feedback),
    )

    system_prompt = get_plan_system_prompt(
        replan_count=session.get_replan_count(),
        error_feedback=error_feedback,
    )

    new_plan: dict[str, Any] = {}
    try:
        config = RunnableConfig(**{"response_format": {"type": "json_object"}})
        response = invoke_llm(
            [SystemMessage(content=system_prompt), *session.messages],
            node="plan_node",
            config=config,
        )
        raw_content = str(getattr(response, "content", "") or "")
        logger.info("[node:plan] LLM raw response (first 500 chars): %s", raw_content[:500])
        text = extract_json_text(raw_content)
        if text:
            new_plan, _ = json.JSONDecoder().raw_decode(text)

        if not new_plan:
            logger.warning("[node:plan] first attempt returned empty plan, retrying without response_format")
            session.messages.append(response)
            response2 = invoke_llm(
                [SystemMessage(content=system_prompt), *session.messages],
                node="plan_node",
            )
            raw_content2 = str(getattr(response2, "content", "") or "")
            logger.info("[node:plan] retry raw response (first 500 chars): %s", raw_content2[:500])
            session.messages.append(response2)
            text2 = extract_json_text(raw_content2)
            if text2:
                new_plan, _ = json.JSONDecoder().raw_decode(text2)
        else:
            session.messages.append(response)

        logger.info("[node:plan] LLM responded, plan keys=%s", list(new_plan.keys()))
    except json.JSONDecodeError as exc:
        logger.warning("plan_node: JSON parse failed: %s, text=%r", exc, text[:200] if text else "")
        new_plan = {}
    except Exception as exc:
        logger.warning("plan_node: LLM plan generation failed: %s", exc, exc_info=True)
        new_plan = {}

    new_plan.setdefault("description", "")
    new_plan.setdefault("subtasks", [])
    new_plan.setdefault("validation_commands", [])
    new_plan.setdefault("constraints", [])
    new_plan.setdefault("error_feedback", "")

    session.set_plan(new_plan)
    session.clear_plan_invalid()
    logger.info("[node:plan] done: desc=%r, subtasks=%d", new_plan.get("description", ""), len(new_plan.get("subtasks", [])))
    writer({"type": "plan_created", "plan": new_plan, "replan_count": session.get_replan_count()})
    return {}


def coding_agent_node(state: DotAgentState) -> dict[str, Any]:
    """编码执行节点：按 task_plan 执行编码，评估 plan 合理性决定路由"""
    session = state["session"]
    ctx = state["context"]
    writer = _get_writer()
    task_plan = session.get_plan()
    logger.info(
        "[node:coding_agent] entering: desc=%r, subtasks=%d, replan=%d/%d",
        task_plan.get("description", ""),
        len(task_plan.get("subtasks", [])),
        session.get_replan_count(), session.replan_max,
    )

    try:
        tools = build_tools_for_session(session, ctx)
    except Exception as exc:
        logger.warning("coding_agent_node: tool build failed: %s", exc, exc_info=True)
        tools = []

    plan_description = task_plan.get("description", "")
    subtasks = task_plan.get("subtasks", [])
    constraints = task_plan.get("constraints", [])

    static_context = load_static_context(session)
    if static_context:
        logger.info("[node:coding_agent] loaded static_context (%d chars)", len(static_context))

    mcp_loaded = set(ctx.loaded_mcp_tools)
    skill_loaded = set(ctx.loaded_skills)

    mcp_catalog = safe_host_call(ctx.mcp_host, "get_catalog_text", "", mcp_loaded)
    mcp_rules = safe_host_call(ctx.mcp_host, "get_system_prompt_rules", "")

    skills_catalog = safe_host_call(ctx.skill_host, "get_catalog_text", "", skill_loaded)
    skills_rules = safe_host_call(ctx.skill_host, "get_system_prompt_rules", "")
    if mcp_catalog:
        logger.info("[node:coding_agent] mcp_catalog (%d tools)", len(mcp_catalog.split("\n")))
    if skills_catalog:
        logger.info("[node:coding_agent] skills_catalog loaded")

    base_system_prompt = get_coding_system_prompt(
        plan_description=plan_description,
        subtasks=subtasks,
        constraints=constraints,
        static_context=static_context,
        mcp_catalog=mcp_catalog,
        mcp_rules=mcp_rules,
        skills_catalog=skills_catalog,
        skills_rules=skills_rules,
    )

    loop_count = 0
    tool_call_count = 0

    while loop_count < CODING_MAX_LOOPS:
        loop_count += 1

        skill_content = ctx.active_skill_content
        system_prompt = base_system_prompt + (f"\n\n{skill_content}" if skill_content else "")
        agent_messages: list[Any] = [SystemMessage(content=system_prompt), *session.messages]

        logger.info("[node:coding_agent] loop=%d/%d, messages_in_context=%d", loop_count, CODING_MAX_LOOPS,
                    len(agent_messages))

        try:
            response = invoke_llm(agent_messages, node="coding_agent", tools=tools)
        except Exception as exc:
            logger.warning("coding_agent_node: model invoke failed: %s", exc, exc_info=True)
            break

        session.messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            logger.info("[node:coding_agent] loop=%d: no tool calls, agent finished", loop_count)
            break

        tool_call_count += len(tool_calls)
        logger.info("[node:coding_agent] loop=%d: %d tool calls", loop_count, len(tool_calls))
        writer({"type": "tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_name = call.get("name", "")
            logger.info("[node:coding_agent] -> tool: %s  args=%s", tool_name, short_args(call.get("args")))
            tool_msg = run_tool_call(session, ctx, tools, call, prefix="call")
            if tool_msg is not None:
                session.messages.append(tool_msg)

    summary = last_ai_content(session.messages) or (
        f"Executed {tool_call_count} tool calls in {loop_count} loops"
    )

    plan_reasonable, plan_issue = evaluate_plan_quality(
        plan_description=plan_description,
        subtasks=subtasks,
        tool_call_count=tool_call_count,
        loop_count=loop_count,
        summary=summary,
    )

    if not plan_reasonable:
        logger.warning("[node:coding_agent] plan INVALID: %s  (replan=%d/%d)", plan_issue, session.get_replan_count(),
                       session.replan_max)
        session.messages.append(AIMessage(content=f"[Plan Issue] {plan_issue}"))
        if session.get_replan_count() < session.replan_max:
            session.mark_replan()
            session.set_plan({**task_plan, "error_feedback": plan_issue})
            session.mark_plan_invalid()
            session.clear_intervene_flag()
            logger.info("[node:coding_agent] -> replan triggered (count=%d)", session.get_replan_count())
        else:
            session.mark_intervene()
            logger.warning("[node:coding_agent] -> need_human_intervene (replan exhausted)")
        writer({"type": "plan_invalid", "issue": plan_issue, "replan_count": session.get_replan_count()})
    else:
        session.clear_intervene_flag()
        logger.info("[node:coding_agent] plan OK -> routing to valid_node")

    return {}


def valid_node(state: DotAgentState) -> dict[str, Any]:
    """校验节点：agent 自主验证编码结果"""
    session = state["session"]
    ctx = state["context"]
    writer = _get_writer()
    task_plan = session.get_plan()
    validation_commands = task_plan.get("validation_commands", [])
    logger.info(
        "[node:valid_node] entering: attempt=%d/%d, plan=%r",
        session.get_attempt_count(), session.max_attempt,
        task_plan.get("description", "")[:80],
    )

    # 纯对话检测
    _last_human_idx = -1
    for _i in range(len(session.messages) - 1, -1, -1):
        if isinstance(session.messages[_i], HumanMessage):
            _last_human_idx = _i
            break
    _has_tool_messages = any(
        isinstance(session.messages[_j], ToolMessage)
        for _j in range(_last_human_idx + 1, len(session.messages))
    )
    if not _has_tool_messages:
        session.set_validate_result({
            "passed": True,
            "error_msg": "",
            "fail_reason": "",
            "checks": [{"name": "chat_only", "passed": True, "detail": "纯对话任务，无需工作区验证"}],
        })
        session.mark_attempt()
        logger.info("[node:valid_node] chat-only task, auto-passed")
        writer({"type": "validation_result", "passed": True, "chat_only": True})
        return {}

    try:
        tools = build_tools_for_session(session, ctx)
    except Exception as exc:
        logger.warning("valid_node: tool build failed: %s", exc, exc_info=True)
        tools = []

    system_prompt = get_valid_system_prompt(
        plan_description=task_plan.get("description", ""),
        subtasks=task_plan.get("subtasks", []),
        validation_commands=validation_commands,
    )

    loop_count = 0
    while loop_count < VALID_MAX_LOOPS:
        loop_count += 1
        agent_messages: list[Any] = [SystemMessage(content=system_prompt), *session.messages]

        try:
            response = invoke_llm(agent_messages, node="valid_node", tools=tools)
        except Exception as exc:
            logger.warning("valid_node: model invoke failed: %s", exc, exc_info=True)
            session.set_validate_result({
                "passed": False,
                "error_msg": f"Model invoke failed: {exc}",
                "fail_reason": f"Model invoke failed: {exc}",
                "checks": [],
            })
            session.mark_attempt()
            writer({"type": "validation_result", "passed": False, "error_msg": str(exc)})
            return {}

        session.messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        writer({"type": "validation_tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_msg = run_tool_call(session, ctx, tools, call, prefix="val")
            if tool_msg is not None:
                session.messages.append(tool_msg)

    session.set_validate_result(parse_validation_result(session.messages))
    session.mark_attempt()
    passed = session.get_validate_result().get("passed", False)
    logger.info(
        "[node:valid_node] result: passed=%s, attempts=%d/%d, reason=%s",
        passed, session.get_attempt_count(), session.max_attempt,
        session.get_validate_result().get("error_msg", "")[:80],
    )
    writer({"type": "validation_result", "passed": passed})
    return {}


def human_intervene_node(state: DotAgentState) -> dict[str, Any]:
    """人工介入节点"""
    session = state["session"]
    writer = _get_writer()
    logger.info(
        "[node:human_intervene] entering: replan=%d, attempt=%d",
        session.get_replan_count(), session.get_attempt_count(),
    )

    session.set_awaiting_intervention(True)
    session.clear_intervene_flag()
    session.set_resume_action("pending")

    writer({
        "type": "human_intervene",
        "value": {
            "reason": "replan_or_attempt_exhausted",
            "replan_count": session.get_replan_count(),
            "attempt_count": session.get_attempt_count(),
        },
    })
    return {}


def finally_node(state: DotAgentState) -> dict[str, Any]:
    """终止节点：收尾 + 持久化"""
    session = state["session"]
    writer = _get_writer()

    passed = session.get_validate_result().get("passed", False)
    summary = session.get_plan().get("description", "") or session.get_task()

    final_answer = last_final_answer(session)

    turn_id = session.current_turn_id + 1
    session.current_turn_id = turn_id
    if session.persistence is not None:
        try:
            from ..session.persistence import persist_turn

            persist_turn(session, turn_id)
        except Exception as exc:
            logger.warning("[node:finally] persist_turn failed: %s", exc, exc_info=True)

    logger.info("[node:finally] passed=%s, turn=%d, summary=%r", passed, turn_id, summary[:80])
    writer({"type": "final", "passed": passed, "summary": summary, "answer": final_answer})
    return {}
