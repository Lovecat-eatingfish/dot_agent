"""
LangGraph 工作流节点实现

本模块实现了 MokioClaw 多智能体工作流的所有节点：

1. intent_router_node - 意图路由器
   - 判断用户输入是聊天还是工作流任务
   - 输出 intent_route, intent_reason, intent_confidence

2. chat_responder_node - 聊天回复器
   - 处理轻量级对话（问候、感谢等）
   - 不访问工作区，直接回复

3. planner_node - 规划器（核心节点）
   - 制定任务计划
   - 委派任务给 search_agent 和 code_agent
   - 管理待办事项

4. context_monitor_node - 上下文监控器
   - 监控消息列表的 token 数量
   - 决定是否需要压缩上下文

5. context_compressor_node - 上下文压缩器
   - 压缩过长的消息列表
   - 保留关键信息，移除冗余内容

6. verifier_node - 校验器
   - 验证任务是否完成
   - 返回结构化的校验结果

7. final_node - 结束节点
   - 生成最终结果摘要
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from mokioclaw.agents.code_agent import run_code_agent
from mokioclaw.agents.search_agent import run_search_agent
from mokioclaw.core.utils import (
    dedupe_sources,
    execute_tool_by_name,
    last_ai_content,
    tool_result_event as build_tool_result_event,
    truncate,
    trim_handoffs as trim_handoffs_util,
)
from mokioclaw.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
    persist_history_summary,
)
from mokioclaw.graph.state import MokioGraphState, TodoItem, VerificationCheck
from mokioclaw.prompts.agent_prompt import (
    CHAT_RESPONDER_PROMPT,
    INTENT_ROUTER_PROMPT,
    PLANNER_PROMPT,
    VERIFIER_PROMPT,
)
from mokioclaw.prompts.context_manager_prompt import CONTEXT_COMPRESSION_PROMPT
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.tools import build_read_only_tools
from mokioclaw.tools.todo_tool import persist_todos, write_todos

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# ========== 默认配置 ==========
DEFAULT_CONTEXT_TOKEN_LIMIT = 400000  # 上下文 token 数量上限

DEFAULT_TODOS = [
    "Clarify the deliverable and acceptance criteria.",
    "Delegate specialist work needed for the task.",
    "Verify the generated result.",
]

def intent_router_node(state: MokioGraphState) -> dict[str, Any]:
    """意图路由器节点

    分析用户输入，判断应该走聊天分支还是工作流分支。

    决策逻辑：
    1. 调用 LLM 分析用户输入
    2. 解析返回的 JSON（route, reason, confidence）
    3. 如果置信度 >= 0.55 且路由有效，则使用 LLM 的决策
    4. 否则默认走工作流分支

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：intent_route, intent_reason, intent_confidence
    """
    writer = _get_writer()
    route = "workflow"
    reason = "router fallback: default to workflow"
    confidence = 0.0
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=INTENT_ROUTER_PROMPT),
                HumanMessage(content=_router_input(state)),
            ]
        )
        parsed = _extract_json(str(response.content)) or {}
        candidate = str(parsed.get("route", "")).strip().lower()
        parsed_confidence = _coerce_confidence(parsed.get("confidence"))
        if candidate in {"chat", "workflow"} and parsed_confidence >= 0.55:
            route = candidate
            confidence = parsed_confidence
            reason = str(parsed.get("reason") or "")
        else:
            reason = str(parsed.get("reason") or "router returned low-confidence or invalid route")
            confidence = parsed_confidence
    except Exception as exc:
        logger.warning("intent_router failed: %s", exc, exc_info=True)
        reason = f"router error: {type(exc).__name__}: {exc}"

    event = {
        "type": "intent_decision",
        "route": route,
        "reason": reason,
        "confidence": confidence,
    }
    writer(event)
    return {
        "intent_route": route,
        "intent_reason": reason,
        "intent_confidence": confidence,
    }


def intent_route_fn(state: MokioGraphState) -> str:
    """根据意图路由决定下一步执行的节点

    Args:
        state: 当前工作流状态

    Returns:
        "chat_responder" 或 "planner"
    """
    return "chat_responder" if state.get("intent_route") == "chat" else "planner"


def chat_responder_node(state: MokioGraphState) -> dict[str, Any]:
    """聊天回复节点

    处理轻量级对话，不调用任何工具，直接返回 LLM 的回复。

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：chat_response, final_answer
    """
    writer = _get_writer()
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=CHAT_RESPONDER_PROMPT),
                HumanMessage(content=_chat_input(state)),
            ]
        )
        text = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        logger.warning("chat_responder failed: %s", exc, exc_info=True)
        text = f"这是轻量聊天分支，但模型回复暂不可用：{type(exc).__name__}: {exc}"
    if not text:
        text = "我在。你可以继续提问，或者直接描述一个需要我完成的任务。"
    event = {
        "type": "chat_response",
        "mode": "lightweight",
        "reason": state.get("intent_reason", ""),
        "response": text,
    }
    writer(event)
    return {"chat_response": text, "final_answer": text}


def planner_node(state: MokioGraphState) -> dict[str, Any]:
    """规划器节点（核心节点）

    职责：
    1. 制定或更新任务计划（todos, acceptance_criteria, verification_commands）
    2. 委派任务给专业智能体（search_agent, code_agent）
    3. 管理待办事项状态

    可用工具：
    - TodoWriteTool: 创建或更新任务计划
    - CallSearchAgentTool: 委派搜索任务
    - CallCodeAgentTool: 委派代码实现任务

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：plan_summary, todos, acceptance_criteria, verification_commands,
        research_notes, sources, agent_handoffs, code_agent_summary, messages 等
    """
    writer = _get_writer()
    working_state: MokioGraphState = {**state}
    if not working_state.get("todos"):
        _apply_plan(working_state, _default_plan(working_state["task"]))
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    memory = build_layered_memory(working_state, node="planner")
    writer(memory_event(memory, node="planner"))
    model = create_model()
    planner = model.bind_tools(_build_planner_tools(working_state, writer))
    messages: list[Any] = [
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=_planner_input(working_state, memory)),
    ]
    produced_messages: list[Any] = []

    writer(
        {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "verification_commands": working_state.get("verification_commands", []),
            "attempts": working_state.get("attempts", 0),
        }
    )

    for _ in range(8):
        response = planner.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = _execute_planner_tool(working_state, writer, call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content="planner stopped after the maximum supervisor tool loop count."))

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = _last_ai_content(produced_messages)
    final_memory = build_layered_memory(working_state, node="planner")
    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "research_notes": working_state.get("research_notes", ""),
        "sources": working_state.get("sources", []),
        "agent_handoffs": working_state.get("agent_handoffs", []),
        "code_agent_summary": working_state.get("code_agent_summary", ""),
        "last_actor_summary": working_state.get("code_agent_summary", ""),
        "messages": produced_messages,
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
    }


def verifier_node(state: MokioGraphState) -> dict[str, Any]:
    """校验器节点

    职责：
    1. 使用只读工具检查工作区状态
    2. 执行验证命令
    3. 判断任务是否完成
    4. 返回结构化的校验结果

    校验结果格式：
    {
        "passed": true/false,
        "reason": "校验说明",
        "checks": [{"name": "...", "passed": true/false, "detail": "..."}],
        "recommended_next_instruction": "失败时的修复建议"
    }

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：verification_results, verification_checks, verifier_summary,
        passed, attempts, last_error, todos
    """
    writer = _get_writer()
    memory = build_layered_memory(state, node="verifier")
    writer(memory_event(memory, node="verifier"))
    writer(
        {
            "type": "plan_snapshot",
            "node": "verifier",
            "plan_summary": state.get("plan_summary", ""),
            "todos": state.get("todos", []),
            "verification_commands": state.get("verification_commands", []),
        }
    )

    model = create_model()
    verifier = model.bind_tools(build_read_only_tools(state["runtime"]))
    messages: list[Any] = [
        SystemMessage(content=VERIFIER_PROMPT),
        HumanMessage(content=_verifier_input(state, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(8):
        response = verifier.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer({"type": "tool_call", "node": "verifier", "name": call.get("name"), "args": call.get("args", {})})
            tool_message = _execute_read_only_tool(state, call)
            event = build_tool_result_event(tool_message, node="verifier")
            tool_events.append(event)
            writer(event)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(
            AIMessage(
                content=json.dumps(
                    {
                        "passed": False,
                        "reason": "Verifier stopped after the maximum tool loop count.",
                        "checks": [],
                        "recommended_next_instruction": "Inspect the workspace and complete the unfinished task.",
                    },
                    ensure_ascii=False,
                )
            )
        )

    parsed = _extract_json(_last_ai_content(produced_messages)) or {
        "passed": False,
        "reason": "Verifier did not return valid JSON.",
        "checks": [
            {
                "name": "verifier_json",
                "passed": False,
                "detail": _last_ai_content(produced_messages)[:800],
            }
        ],
        "recommended_next_instruction": "Return valid verifier JSON after inspecting the result.",
    }
    checks = _normalize_checks(parsed.get("checks"))
    passed = bool(parsed.get("passed"))
    reason = str(parsed.get("reason") or "")
    recommended = str(parsed.get("recommended_next_instruction") or "")
    attempts = state.get("attempts", 0) + 1
    todos = [dict(todo) for todo in state.get("todos", [])]
    if passed:
        todos = [
            {
                **todo,
                "status": "completed" if todo.get("status") != "blocked" else todo.get("status", "blocked"),
                "note": todo.get("note") or "verified",
            }
            for todo in todos
        ]
        writer(
            {
                "type": "todo_update",
                "node": "verifier",
                "plan_summary": state.get("plan_summary", ""),
                "todos": todos,
                "verification_commands": state.get("verification_commands", []),
            }
        )
    last_error = "" if passed else _format_verifier_error(reason, recommended, tool_events)

    return {
        "messages": produced_messages,
        "verification_results": _tool_events_to_verification_results(tool_events),
        "verification_checks": checks,
        "verifier_summary": reason,
        "passed": passed,
        "attempts": attempts,
        "last_error": last_error,
        "todos": todos,
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get("history_summary", ""),
        "context_next_node": verifier_route({**state, "passed": passed, "attempts": attempts}),
    }


def context_monitor_node(state: MokioGraphState) -> dict[str, Any]:
    """上下文监控节点

    职责：
    1. 估算当前消息列表的 token 数量
    2. 与配置的上限进行比较
    3. 决定是否需要压缩上下文

    监控指标：
    - token_count: 当前 token 数量
    - token_limit: 配置的上限（默认 400000）
    - should_compress: 是否需要压缩
    - message_count: 当前消息数量

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：context_token_count, context_token_limit,
        context_should_compress, context_next_node
    """
    writer = _get_writer()
    token_limit = get_context_token_limit()
    token_count = estimate_context_tokens(state)
    should_compress = token_count >= token_limit
    next_node = state.get("context_next_node") or "verifier"
    event = {
        "type": "context_monitor",
        "token_count": token_count,
        "token_limit": token_limit,
        "should_compress": should_compress,
        "next_node": next_node,
        "message_count": len(state.get("messages", [])),
    }
    writer(event)
    return {
        "context_token_count": token_count,
        "context_token_limit": token_limit,
        "context_should_compress": should_compress,
        "context_next_node": next_node,
    }


def context_monitor_route(state: MokioGraphState) -> str:
    """上下文监控路由函数

    根据上下文状态决定下一步：
    - context_compressor: 需要压缩上下文
    - verifier: 正常流程，进入校验
    - planner: 需要重新规划
    - final: 任务完成

    Args:
        state: 当前工作流状态

    Returns:
        下一个节点的名称
    """
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))
    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))
    compressed = _compress_context_with_model(state)
    summary = _format_compressed_context(compressed, state)
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    post_state: MokioGraphState = {
        **state,
        "messages": [summary_message],
        "context_summary": summary,
        "history_summary": summary,
        "memory_snapshot": build_layered_memory(
            {**state, "context_summary": summary, "history_summary": summary},
            node="context_compressor",
        ),
        "research_notes": _short_text(state.get("research_notes", ""), 1200),
        "agent_handoffs": _trim_handoffs(state.get("agent_handoffs", [])),
        "last_error": _short_text(state.get("last_error", ""), 1600),
        "code_agent_summary": _short_text(state.get("code_agent_summary", ""), 1200),
        "verifier_summary": _short_text(state.get("verifier_summary", ""), 1200),
    }
    after_tokens = estimate_context_tokens(post_state)
    compression_event = {
        "before_tokens": int(before_tokens),
        "after_tokens": int(after_tokens),
        "removed_messages": len(before_messages),
        "summary": _short_text(summary, 1200),
        "next_node": state.get("context_next_node", "verifier"),
    }
    events = list(state.get("compression_events", [])) + [compression_event]
    writer({"type": "context_compression", **compression_event})
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_message],
        "context_summary": summary,
        "context_token_count": after_tokens,
        "context_should_compress": False,
        "research_notes": post_state.get("research_notes", ""),
        "agent_handoffs": post_state.get("agent_handoffs", []),
        "last_error": post_state.get("last_error", ""),
        "code_agent_summary": post_state.get("code_agent_summary", ""),
        "verifier_summary": post_state.get("verifier_summary", ""),
        "memory_snapshot": post_state.get("memory_snapshot", {}),
        "history_summary": summary,
        "compression_events": events,
    }


def context_compressor_route(state: MokioGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: MokioGraphState) -> str:
    """校验器路由函数

    根据校验结果决定下一步：
    - final: 校验通过或达到最大重试次数
    - planner: 校验失败，需要重新规划

    Args:
        state: 当前工作流状态

    Returns:
        下一个节点的名称
    """
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: MokioGraphState) -> dict[str, Any]:
    """结束节点

    生成任务执行的最终结果摘要，包括：
    - 任务状态（PASSED/FAILED）
    - 计划摘要
    - 待办事项列表
    - 研究来源
    - 校验结果
    - 上下文压缩统计
    - 代码智能体摘要

    Args:
        state: 当前工作流状态

    Returns:
        包含 final_answer 的状态更新
    """
    status = "PASSED" if state.get("passed") else "FAILED"
    checks = "\n".join(
        f"- {check.get('name', 'check')}: {'PASS' if check.get('passed') else 'FAIL'} - {check.get('detail', '')}"
        for check in state.get("verification_checks", [])
    )
    todos = "\n".join(f"- [{todo.get('status', '')}] {todo.get('content', '')}" for todo in state.get("todos", []))
    sources = "\n".join(f"- {source.get('title', '')}: {source.get('url', '')}" for source in state.get("sources", []))
    compression_events = state.get("compression_events", [])
    compression_text = "(none)"
    if compression_events:
        latest = compression_events[-1]
        compression_text = (
            f"{len(compression_events)} compression(s); "
            f"latest {latest.get('before_tokens')} -> {latest.get('after_tokens')} tokens; "
            f"removed {latest.get('removed_messages')} message(s)"
        )
    final_answer = (
        f"LangGraph MultiAgent workflow finished: {status}\n\n"
        f"Plan: {state.get('plan_summary', '')}\n\n"
        f"Todos:\n{todos}\n\n"
        f"Research sources:\n{sources or '(none)'}\n\n"
        f"Verifier:\n{state.get('verifier_summary', '')}\n\n"
        f"Checks:\n{checks or '(none)'}\n\n"
        f"Context compression:\n{compression_text}\n\n"
        f"CodeAgent summary:\n{state.get('code_agent_summary') or state.get('last_actor_summary', '')}"
    )
    return {"final_answer": final_answer}


def get_context_token_limit() -> int:
    raw = os.getenv("MOKIO_CONTEXT_TOKEN_LIMIT", str(DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: MokioGraphState) -> int:
    messages = list(state.get("messages", []))
    payload = build_layered_memory(state, node="context_monitor")
    payload_message = HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))
    try:
        model = create_model()
        return int(model.get_num_tokens_from_messages(messages + [payload_message]))
    except Exception as exc:
        logger.debug("token estimation fallback (model unavailable): %s", exc)
        text = "\n".join(_message_text(message) for message in messages)
        text += "\n" + payload_message.content
        return max(1, len(text) // 4)


def _build_planner_tools(state: MokioGraphState, writer) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="TodoWriteTool",
            func=lambda todos, acceptance_criteria, verification_commands, plan_summary="": _todo_write_tool(
                state, writer, todos, acceptance_criteria, verification_commands, plan_summary
            ),
            description=(
                "Publish or revise plan state. Args: todos, acceptance_criteria, "
                "verification_commands, optional plan_summary."
            ),
        ),
        StructuredTool.from_function(
            name="CallSearchAgentTool",
            func=lambda instruction: _call_search_agent_tool(state, writer, instruction),
            description="Delegate research work to searchAgent. Args: instruction.",
        ),
        StructuredTool.from_function(
            name="CallCodeAgentTool",
            func=lambda instruction: _call_code_agent_tool(state, writer, instruction),
            description="Delegate implementation work to codeAgent. Args: instruction.",
        ),
    ]


def _todo_write_tool(
    state: MokioGraphState,
    writer,
    todos: Any,
    acceptance_criteria: Any,
    verification_commands: Any,
    plan_summary: str = "",
) -> dict[str, Any]:
    result = write_todos(todos, acceptance_criteria, verification_commands)
    if result.get("ok"):
        state["plan_summary"] = plan_summary or state.get("plan_summary") or "MultiAgent plan"
        state["todos"] = _todo_items(result["todos"], existing=state.get("todos", []))
        state["acceptance_criteria"] = result["acceptance_criteria"]
        state["verification_commands"] = result["verification_commands"]
        persist_todos(
            state["runtime"],
            state["todos"],
            state["acceptance_criteria"],
            state["verification_commands"],
            state.get("plan_summary", ""),
        )
        writer(
            {
                "type": "plan_snapshot",
                "node": "planner",
                "plan_summary": state.get("plan_summary", ""),
                "todos": state.get("todos", []),
                "verification_commands": state.get("verification_commands", []),
                "acceptance_criteria": state.get("acceptance_criteria", []),
            }
        )
    return {
        **result,
        "plan_summary": state.get("plan_summary", ""),
        "todo_items": state.get("todos", []),
    }


def _call_search_agent_tool(state: MokioGraphState, writer, instruction: str) -> dict[str, Any]:
    writer({"type": "handoff", "from": "planner", "to": "searchAgent", "instruction": instruction})
    result = run_search_agent(state, instruction, writer=writer)
    existing_sources = list(state.get("sources", []))
    state["research_notes"] = _join_notes(state.get("research_notes", ""), result.get("summary", ""))
    state["sources"] = _dedupe_sources(existing_sources + list(result.get("sources", [])))
    handoff = {
        "from_agent": "planner",
        "to_agent": "searchAgent",
        "instruction": instruction,
        "result": result.get("summary", ""),
    }
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
    writer({"type": "handoff_result", "from": "searchAgent", "to": "planner", "result": result.get("summary", "")})
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "sources": state.get("sources", []),
        "queries": result.get("queries", []),
    }


def _call_code_agent_tool(state: MokioGraphState, writer, instruction: str) -> dict[str, Any]:
    writer({"type": "handoff", "from": "planner", "to": "codeAgent", "instruction": instruction})
    result = run_code_agent(state, instruction, writer=writer)
    state["todos"] = result.get("todos", state.get("todos", []))
    state["code_agent_summary"] = result.get("summary", "")
    state["last_actor_summary"] = result.get("summary", "")
    handoff = {
        "from_agent": "planner",
        "to_agent": "codeAgent",
        "instruction": instruction,
        "result": result.get("summary", ""),
    }
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
    writer({"type": "handoff_result", "from": "codeAgent", "to": "planner", "result": result.get("summary", "")})
    return {"ok": True, "summary": result.get("summary", ""), "todos": state.get("todos", [])}


def _execute_planner_tool(state: MokioGraphState, writer, call: dict[str, Any]) -> ToolMessage:
    """执行 planner 的工具调用（带 writer 事件输出）"""
    name = call.get("name", "")
    args = call.get("args") or {}
    writer({"type": "tool_call", "node": "planner", "name": name, "args": args})
    tool_message = execute_tool_by_name(_build_planner_tools(state, writer), call)
    writer(build_tool_result_event(tool_message, node="planner"))
    return tool_message


def _execute_read_only_tool(state: MokioGraphState, call: dict[str, Any]) -> ToolMessage:
    """执行只读工具调用"""
    return execute_tool_by_name(build_read_only_tools(state["runtime"]), call)


def _compress_context_with_model(state: MokioGraphState) -> dict[str, Any]:
    memory = build_layered_memory(state, node="context_compressor")
    payload = {
        "context_summary": state.get("context_summary", ""),
        "memory": memory,
        "messages": [_message_snapshot(message) for message in state.get("messages", [])],
    }
    messages = [
        SystemMessage(content=CONTEXT_COMPRESSION_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ]
    try:
        response = create_model().invoke(messages)
        parsed = _extract_json(str(response.content))
        if parsed:
            return parsed
    except Exception as exc:
        logger.warning("context compression failed: %s", exc, exc_info=True)
        return _fallback_compression(state, error=f"{type(exc).__name__}: {exc}")
    return _fallback_compression(state, error="compressor model did not return valid JSON")


def _fallback_compression(state: MokioGraphState, *, error: str = "") -> dict[str, Any]:
    return {
        "summary": _short_text(
            "\n\n".join(
                [
                    state.get("context_summary", ""),
                    state.get("research_notes", ""),
                    state.get("code_agent_summary", ""),
                    state.get("verifier_summary", ""),
                    state.get("last_error", ""),
                ]
            ),
            2400,
        ),
        "active_goal": state.get("task", ""),
        "completed_work": state.get("code_agent_summary", ""),
        "open_todos": [
            todo.get("content", "")
            for todo in state.get("todos", [])
            if todo.get("status") != "completed"
        ],
        "important_files": _important_files_from_state(state),
        "tool_findings": _short_text(state.get("last_error", ""), 1200),
        "sources": [{"title": source.get("title", ""), "url": source.get("url", "")} for source in state.get("sources", [])],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _format_compressed_context(compressed: dict[str, Any], state: MokioGraphState) -> str:
    payload = {
        "type": "mokio_context_summary",
        "task": state.get("task", ""),
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "attempts": state.get("attempts", 0),
        "passed": state.get("passed"),
        "compression": compressed,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _context_payload(state: MokioGraphState) -> dict[str, Any]:
    return build_layered_memory(state, node="graph")


def _message_snapshot(message: Any) -> dict[str, str]:
    return {
        "type": type(message).__name__,
        "name": str(getattr(message, "name", "") or ""),
        "content": _short_text(_message_text(message), 2000),
    }


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _important_files_from_state(state: MokioGraphState) -> list[str]:
    files: list[str] = []
    for command in state.get("verification_commands", []):
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", command))
    for text in [state.get("code_agent_summary", ""), state.get("last_error", "")]:
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", text))
    seen: set[str] = set()
    deduped = []
    for item in files:
        normalized = item.strip("\"'")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _planner_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn coding session:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)


def _verifier_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [f"Task: {state['task']}"]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn coding session:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    parts.append("Inspect the workspace with tools and return only verifier JSON.")
    return "\n\n".join(parts)


def _router_input(state: MokioGraphState) -> str:
    """构建路由器/聊天输入提示词"""
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


_chat_input = _router_input


def _default_plan(task: str) -> dict[str, Any]:
    return {
        "plan_summary": "Coordinate specialist agents to complete and verify the requested deliverable.",
        "todos": DEFAULT_TODOS,
        "acceptance_criteria": ["The requested deliverable exists.", "The verifier model confirms completion."],
        "verification_commands": [],
    }


def _apply_plan(state: MokioGraphState, plan: dict[str, Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = _todo_items([str(item) for item in plan.get("todos", [])], existing=state.get("todos", []))
    state["acceptance_criteria"] = [str(item) for item in plan.get("acceptance_criteria", [])]
    state["verification_commands"] = _verification_commands_for_task(state["task"], plan)


def _verification_commands_for_task(task: str, parsed: dict[str, Any]) -> list[str]:
    return [str(item) for item in parsed.get("verification_commands") or []]


def _todo_items(todos: list[str], *, existing: list[dict[str, Any]] | None = None) -> list[TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[TodoItem] = []
    for idx, todo in enumerate(todos, start=1):
        previous = existing_by_content.get(todo, {})
        items.append(
            {
                "id": str(previous.get("id") or f"todo-{idx}"),
                "content": todo,
                "status": str(previous.get("status") or "pending"),
                "note": str(previous.get("note") or ""),
            }
        )
    return items


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(raw, start)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _tool_events_to_verification_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for event in events:
        result = event.get("result", {})
        if not isinstance(result, dict):
            continue
        results.append(
            {
                "command": result.get("command") or event.get("name", ""),
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr") or result.get("error", "")),
            }
        )
    return results


def _normalize_checks(raw: Any) -> list[VerificationCheck]:
    if not isinstance(raw, list):
        return []
    checks: list[VerificationCheck] = []
    for item in raw:
        if isinstance(item, dict):
            checks.append(
                {
                    "name": str(item.get("name") or "check"),
                    "passed": bool(item.get("passed")),
                    "detail": str(item.get("detail") or ""),
                }
            )
    return checks


def _format_verifier_error(reason: str, recommended: str, tool_events: list[dict[str, Any]]) -> str:
    event_text = json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )


def _join_notes(existing: str, new: str) -> str:
    if not existing:
        return new
    if not new:
        return existing
    return existing + "\n\n" + new


_dedupe_sources = dedupe_sources


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """截断智能体交接记录"""
    return trim_handoffs_util(handoffs)


def _short_text(text: str, limit: int) -> str:
    """截断文本到指定长度"""
    return truncate(text, limit)


_last_ai_content = last_ai_content


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
