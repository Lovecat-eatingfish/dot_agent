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
    sanitize_user_input,
    tool_result_event as build_tool_result_event,
    truncate,
    trim_handoffs as trim_handoffs_util,
)
from mokioclaw.core.events import get_event_bus
from mokioclaw.memory.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
    persist_history_summary,
)
from mokioclaw.state.graph import MokioGraphState, TodoItem, VerificationCheck
from mokioclaw.memory.tiered_compression import compress_messages_by_tier, estimate_tokens_for_tiered_compression
from mokioclaw.memory.dual_threshold_compression import (
    CompressionThresholds,
    DualThresholdCompressor,
)
from mokioclaw.prompts.agent_prompt import (
    CHAT_RESPONDER_PROMPT,  # noqa: F401 — kept for backward compat, used via builder
    CODE_AGENT_PROMPT,      # noqa: F401 — used in code_agent.py
    INTENT_ROUTER_PROMPT,   # noqa: F401 — kept for backward compat, used via builder
    PLANNER_PROMPT,         # noqa: F401 — kept for backward compat, used via builder
    SEARCH_AGENT_PROMPT,    # noqa: F401 — used in search_agent.py
    VERIFIER_PROMPT,        # noqa: F401 — kept for backward compat, used via builder
)
from mokioclaw.prompts.builder import PromptBuilder, get_prompt_builder
from mokioclaw.prompts.context_manager_prompt import CONTEXT_COMPRESSION_PROMPT  # noqa: F401 — used via builder
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


def _get_prompt_builder(state: MokioGraphState) -> PromptBuilder:
    """从 state 的 workspace / runtime 构建 PromptBuilder"""
    runtime = state.get("runtime")
    workspace = runtime.workspace if runtime else None
    return get_prompt_builder(workspace=workspace, runtime=runtime)

def intent_router_node(state: MokioGraphState) -> dict[str, Any]:
    """意图路由器节点（轻量启发式，默认工具驱动 workflow）

    对齐 docs/agent.md：去掉重型意图 LLM，由规则快速分流。
    仅当启发式置信度不足时，才可选地回退到 LLM（MOKIO_INTENT_LLM=1）。
    """
    import os

    from mokioclaw.orchestration.intent import classify_intent

    writer = _get_writer()
    task = str(state.get("task") or "")
    route, reason, confidence = classify_intent(task)

    use_llm = os.getenv("MOKIO_INTENT_LLM", "").strip() in {"1", "true", "yes"} and confidence < 0.8
    if use_llm:
        builder = _get_prompt_builder(state)
        try:
            response = create_model().invoke(
                [
                    SystemMessage(content=builder.build("intent_router")),
                    HumanMessage(content=_router_input(state)),
                ]
            )
            parsed = _extract_json(str(response.content)) or {}
            candidate = str(parsed.get("route", "")).strip().lower()
            parsed_confidence = _coerce_confidence(parsed.get("confidence"))
            if candidate in {"chat", "workflow"} and parsed_confidence >= 0.55:
                route = candidate
                confidence = parsed_confidence
                reason = str(parsed.get("reason") or reason)
        except Exception as exc:
            logger.warning("intent_router llm fallback failed: %s", exc, exc_info=True)

    event = {
        "type": "intent_decision",
        "route": route,
        "reason": reason,
        "confidence": confidence,
        "method": "llm" if use_llm else "heuristic",
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


def planner_route(state: MokioGraphState) -> str:
    """根据 planner 的路由决策决定下一步执行的节点

    Args:
        state: 当前工作流状态

    Returns:
        "search_agent" / "code_agent" / "verifier" / "final" / "planner" / "repair"
    """
    route = state.get("planner_route", "verify")
    return {
        "search": "search_agent",
        "code": "code_agent",
        "verify": "verifier",
        "final": "final",
        "replan": "planner",
        "repair": "repair",
    }.get(route, "verifier")


def chat_responder_node(state: MokioGraphState) -> dict[str, Any]:
    """聊天回复节点

    处理轻量级对话，不调用任何工具，直接返回 LLM 的回复。

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：chat_response, final_answer
    """
    writer = _get_writer()
    builder = _get_prompt_builder(state)
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=builder.build("chat_responder")),
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
    """规划器节点（轻量化）

    职责：
    1. 制定或更新任务计划（todos, acceptance_criteria, verification_commands）
    2. 决定下一步路由（search_agent / code_agent / verifier / final / replan）
    3. 不直接调用子 Agent，由图上的独立节点执行

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：plan_summary, todos, acceptance_criteria, verification_commands,
        planner_route, planner_route_instruction, messages 等
    """
    writer = _get_writer()
    builder = _get_prompt_builder(state)
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

    try:
        model = create_model()
        response = model.invoke(
            [
                SystemMessage(content=builder.build("planner")),
                HumanMessage(content=_planner_input(working_state, memory)),
            ]
        )
    except Exception as exc:
        logger.error("planner invoke failed: %s", exc, exc_info=True)
        return {
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "acceptance_criteria": working_state.get("acceptance_criteria", []),
            "verification_commands": working_state.get("verification_commands", []),
            "messages": [AIMessage(content=f"planner invocation error: {exc}")],
            "context_next_node": "verifier",
            "planner_route": "verify",
            "planner_route_instruction": "",
        }

    content = _last_ai_content([response])
    parsed = _extract_json(content)
    if parsed:
        if parsed.get("plan_summary"):
            working_state["plan_summary"] = parsed["plan_summary"]
        if parsed.get("todos"):
            working_state["todos"] = _todo_items(
                [str(item) for item in parsed["todos"]],
                existing=working_state.get("todos", []),
            )
        if parsed.get("acceptance_criteria"):
            working_state["acceptance_criteria"] = [str(item) for item in parsed["acceptance_criteria"]]
        if parsed.get("verification_commands"):
            working_state["verification_commands"] = [str(item) for item in parsed["verification_commands"]]
        working_state["planner_route"] = parsed.get("route", "verify")
        working_state["planner_route_instruction"] = parsed.get("route_instruction", "")

    persist_todos(
        working_state["runtime"],
        working_state.get("todos", []),
        working_state.get("acceptance_criteria", []),
        working_state.get("verification_commands", []),
        working_state.get("plan_summary", ""),
    )

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = content
    final_memory = build_layered_memory(working_state, node="planner")

    # plan 模式：写出 id_todo.md，停止执行，等待用户确认
    runtime = working_state.get("runtime")
    if runtime is not None and getattr(runtime, "agent_mode", "auto") == "plan":
        plan_path = _write_plan_todo_file(runtime, working_state)
        writer(
            {
                "type": "plan_mode_waiting",
                "path": str(plan_path),
                "message": "Plan written. Confirm then switch /mode auto (or approve) to execute.",
            }
        )
        return {
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "acceptance_criteria": working_state.get("acceptance_criteria", []),
            "verification_commands": working_state.get("verification_commands", []),
            "messages": [response],
            "memory_snapshot": final_memory,
            "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
            "metadata": metadata,
            "planner_route": "final",
            "planner_route_instruction": (
                f"Plan-only mode: plan saved to {plan_path}. Awaiting user confirmation."
            ),
            "final_answer": (
                f"Plan ready (agent_mode=plan). Written to `{plan_path}`.\n"
                "Review the plan, then run `/mode auto` (or `/mode approve`) and continue."
            ),
        }

    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "messages": [response],
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
        "planner_route": working_state.get("planner_route", "verify"),
        "planner_route_instruction": working_state.get("planner_route_instruction", ""),
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
    builder = _get_prompt_builder(state)
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

    try:
        model = create_model()
        verifier = model.bind_tools(build_read_only_tools(state["runtime"]))
    except Exception as exc:
        logger.error("verifier model init failed: %s", exc, exc_info=True)
        attempts = state.get("attempts", 0) + 1
        return {
            "messages": [AIMessage(content=f"verifier model unavailable: {exc}")],
            "passed": False,
            "attempts": attempts,
            "last_error": f"Verifier model init failed: {exc}",
            "context_next_node": verifier_route({**state, "passed": False, "attempts": attempts}),
        }

    messages: list[Any] = [
        SystemMessage(content=builder.build("verifier")),
        HumanMessage(content=_verifier_input(state, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(8):
        try:
            response = verifier.invoke(messages)
        except Exception as exc:
            logger.error("verifier invoke failed: %s", exc, exc_info=True)
            produced_messages.append(AIMessage(content=f"verifier invocation error: {exc}"))
            break
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
    checks = _merge_acceptance_gate_checks(state, checks)
    passed = bool(parsed.get("passed")) and _checks_passed(checks)
    reason = str(parsed.get("reason") or "")
    recommended = str(parsed.get("recommended_next_instruction") or "")
    if not passed and not recommended:
        recommended = _recommended_from_failed_checks(checks)
    if not reason:
        reason = _summarize_checks(checks, passed)
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
        "repair_instruction": recommended if not passed else "",
        "todos": todos,
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get("history_summary", ""),
        "context_next_node": verifier_route({**state, "passed": passed, "attempts": attempts}),
    }


def context_monitor_node(state: MokioGraphState) -> dict[str, Any]:
    """上下文监控节点（增强版：双阈值 + 步数触发）

    职责：
    1. 估算当前消息列表的 token 数量
    2. 应用双阈值策略（软阈值预生成 + 硬阈值强制压缩）
    3. 检查工具调用步数，防止无限循环
    4. 决定是否需要压缩上下文

    监控指标：
    - token_count: 当前 token 数量
    - token_limit: 配置的上限（默认 400000）
    - should_compress: 是否需要压缩
    - compression_strategy: 压缩策略（none/soft/hard/step_triggered）
    - message_count: 当前消息数量

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段
    """
    writer = _get_writer()
    token_limit = get_context_token_limit()
    try:
        token_count = estimate_context_tokens(state)
    except Exception as exc:
        logger.warning("context monitor estimation failed, using fallback: %s", exc)
        messages = state.get("messages", [])
        text = "\n".join(_message_text(m) for m in messages)
        # CJK-aware fallback: CJK chars ~1.5 tokens each, ASCII ~0.25 tokens each
        cjk_count = sum(1 for ch in text if '一' <= ch <= '鿿')
        ascii_count = len(text) - cjk_count
        token_count = max(1, int(cjk_count * 1.5 + ascii_count * 0.25))

    # 初始化双阈值压缩器（生产环境应持久化到状态）
    thresholds = CompressionThresholds(
        soft_threshold=0.70,
        hard_threshold=0.90,
        max_context_tokens=token_limit,
    )
    compressor = DualThresholdCompressor(thresholds=thresholds)

    # 统计工具调用步数
    step_count = _count_tool_calls(state)

    # 检查压缩需求
    should_compress, reason, stats = compressor.check_compression_needed(
        current_tokens=token_count,
        step_count=step_count,
    )

    runtime = state.get("runtime")
    updates: dict[str, Any] = {}

    # L2/L3 微压缩：清理过期 read 结果（0 成本）
    try:
        from mokioclaw.memory.microcompact import microcompact_messages
        file_state_map = getattr(runtime, "file_state_map", {}) if runtime else {}
        compacted = microcompact_messages(list(state.get("messages", [])), file_state_map)
        if compacted is not state.get("messages"):
            updates["messages"] = compacted
    except Exception as exc:
        logger.debug("microcompact skipped: %s", exc)

    # Snip 层（对齐 Claude Code HISTORY_SNIP）：裁旧 tool_result，0 成本
    try:
        from mokioclaw.memory.snip import snip_compact_if_needed

        base_msgs = updates.get("messages") or list(state.get("messages", []))
        snipped, tokens_freed = snip_compact_if_needed(base_msgs)
        if tokens_freed > 0:
            updates["messages"] = snipped
            updates["snip_tokens_freed"] = tokens_freed
    except Exception as exc:
        logger.debug("snip skipped: %s", exc)

    # Autocompact 连续失败 → 强制 reactive compact（对齐 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3）
    if runtime is not None and int(getattr(runtime, "autocompact_failures", 0) or 0) >= 3:
        should_compress = True
        reason = "reactive_compact_after_failures"
        stats.strategy = "reactive"

    # /compact 或 runtime.force_compact → 强制走 compressor
    if runtime is not None and getattr(runtime, "force_compact", False):
        should_compress = True
        reason = "user_requested_compact"
        stats.strategy = "hard"
        runtime.force_compact = False

    # PreCompact hook（对齐 Claude Code；matcher 区分 manual/auto）
    if should_compress and runtime is not None:
        try:
            from mokioclaw.core.hooks import HookEvent, HookPayload

            trigger = "manual" if reason == "user_requested_compact" else "auto"
            runtime.hook_runner.run(
                HookEvent.PreCompact,
                HookPayload(
                    event=HookEvent.PreCompact,
                    workspace=str(runtime.workspace),
                    compact_trigger=trigger,
                ),
            )
        except Exception as exc:
            logger.debug("PreCompact hook skipped: %s", exc)

    next_node = state.get("context_next_node") or "verifier"
    event = {
        "type": "context_monitor",
        "token_count": token_count,
        "token_limit": token_limit,
        "should_compress": should_compress,
        "compression_reason": reason,
        "compression_strategy": stats.strategy,
        "next_node": next_node,
        "message_count": len(state.get("messages", [])),
        "step_count": step_count,
    }
    writer(event)

    return {
        **updates,
        "context_token_count": token_count,
        "context_token_limit": token_limit,
        "context_should_compress": should_compress,
        "context_compression_strategy": stats.strategy,
        "context_compression_reason": reason,
        "context_next_node": next_node,
        "step_count": step_count,
    }


def _count_tool_calls(state: MokioGraphState) -> int:
    """统计最近一次会话中的工具调用步数

    用于触发"步数超过5步强制总结"机制

    Args:
        state: 当前状态

    Returns:
        工具调用次数
    """
    messages = state.get("messages", [])
    count = 0
    for msg in messages:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            count += len(msg.tool_calls)
    return count


def context_monitor_route(state: MokioGraphState) -> str:
    """上下文监控路由函数（增强版）

    根据上下文状态决定下一步：
    - context_compressor: 需要压缩上下文
    - verifier: 正常流程，进入校验
    - planner: 需要重新规划
    - final: 任务完成

    支持触发原因：
    - token 超限（硬阈值/软阈值）
    - 工具调用步数过多（>5步）

    Args:
        state: 当前工作流状态

    Returns:
        下一个节点的名称
    """
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: MokioGraphState) -> dict[str, Any]:
    """上下文压缩器节点（增强版：双阈值 + 增量更新 + 完整历史存档）

    压缩策略：
    1. hard（硬阈值）：全量压缩，清除所有消息，生成新摘要，只保留摘要 + 最近 10 条
    2. incremental（软阈值/步数触发）：增量压缩，叠加到旧摘要上，保留最近 20 条
    3. step_triggered：同 incremental，但在 compression_events 中标记触发原因

    面试官考察点对应：
    - Q7: "第11轮怎么处理" → 增量叠加，非全量重算
    - Q8: "前10轮原始上下文" → 持久化到 RAW_HISTORY.md，但不发送给模型
    - Q9: "是增量还是全量" → 增量 O(n)，全量 O(n²)
    - Q4: "什么时候触发" → 双阈值 + 步数触发

    Args:
        state: 当前工作流状态

    Returns:
        压缩后的状态更新
    """
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))

    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))

    # 获取压缩策略（由 context_monitor 的 DualThresholdCompressor 判定）
    strategy = state.get("context_compression_strategy", "hard")
    is_incremental = strategy in ("soft", "step_triggered")
    runtime = state.get("runtime")

    # Reactive：autocompact 连续失败后的激进丢弃，不再调 LLM
    if strategy == "reactive" or (
        runtime is not None and int(getattr(runtime, "autocompact_failures", 0) or 0) >= 3
    ):
        from mokioclaw.memory.snip import reactive_compact_messages

        remaining = reactive_compact_messages(before_messages, keep_last=8)
        if runtime is not None:
            runtime.autocompact_failures = 0
        writer({
            "type": "context_compression",
            "strategy": "reactive",
            "before_messages": len(before_messages),
            "after_messages": len(remaining),
        })
        return {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *remaining],
            "context_should_compress": False,
            "context_compression_strategy": "reactive",
            "context_token_count": estimate_context_tokens({**state, "messages": remaining}),
        }

    # 1. 持久化完整历史（用于审计和摘要重建）
    _persist_raw_history(state["runtime"], before_messages)

    # 2. LLM 压缩（生成结构化摘要）；失败则累加 autocompact_failures
    try:
        compressed = _compress_context_with_model(state)
        summary = _format_compressed_context(compressed, state)
        if runtime is not None:
            runtime.autocompact_failures = 0
    except Exception as exc:
        logger.warning("autocompact failed: %s", exc, exc_info=True)
        if runtime is not None:
            runtime.autocompact_failures = int(getattr(runtime, "autocompact_failures", 0) or 0) + 1
        # 本轮降级为 force snip，下次达 3 次走 reactive
        from mokioclaw.memory.microcompact import force_compact_messages

        remaining = force_compact_messages(before_messages, keep_last=10)
        return {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *remaining],
            "context_should_compress": False,
            "context_compression_strategy": "force_fallback",
            "context_token_count": estimate_context_tokens({**state, "messages": remaining}),
        }
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    # 3. 根据策略决定保留多少消息
    if is_incremental:
        # 增量压缩：保留旧摘要 + 更多最近消息
        kept_messages = before_messages[-20:]  # 保留最近 20 条
        remaining_messages = [summary_message] + kept_messages
    else:
        # 全量压缩（hard）：只保留摘要 + 最近 10 条
        remaining_messages = [summary_message]
        if len(before_messages) > 10:
            remaining_messages.extend(before_messages[-10:])

    # 4. 分级压缩（仅对硬阈值且消息很多时）
    if not is_incremental and len(before_messages) > 20:
        context_summary = state.get("context_summary", "")
        compressed_messages = compress_messages_by_tier(
            before_messages,
            context_summary=context_summary,
        )
        remaining_messages = [summary_message] + compressed_messages[:10]
        tier_stats = estimate_tokens_for_tiered_compression(
            before_messages,
            context_summary,
            compressed_messages=compressed_messages,
        )
        logger.info(
            "tiered compression: %d -> %d messages, tokens: %d -> %d (%.1f%% reduction)",
            len(before_messages),
            len(remaining_messages),
            tier_stats["original_tokens"],
            tier_stats["compressed_tokens"],
            tier_stats["reduction_pct"],
        )

    post_state: MokioGraphState = {
        **state,
        "messages": remaining_messages,
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
        "strategy": strategy,
    }
    events = list(state.get("compression_events", [])) + [compression_event]
    writer({"type": "context_compression", **compression_event})
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)] + remaining_messages,
        "context_summary": summary,
        "context_token_count": after_tokens,
        "context_compression_strategy": strategy,
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


def search_agent_node(state: MokioGraphState) -> dict[str, Any]:
    """搜索智能体节点（图上的独立节点）

    职责：
    1. 读取 planner 的路由指令
    2. 执行 searchAgent
    3. 更新 research_notes / sources / agent_handoffs

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：research_notes, sources, agent_handoffs, search_agent_summary, messages
    """
    writer = _get_writer()
    instruction = state.get("planner_route_instruction", "") or state.get("task", "")
    writer({"type": "handoff", "from": "planner", "to": "searchAgent", "instruction": instruction})
    result = run_search_agent(state, instruction, writer=writer)

    existing_sources = list(state.get("sources", []))
    research_notes = _join_notes(state.get("research_notes", ""), result.get("summary", ""))
    sources = _dedupe_sources(existing_sources + list(result.get("sources", [])))
    handoff = {
        "from_agent": "planner",
        "to_agent": "searchAgent",
        "instruction": instruction,
        "result": result.get("summary", ""),
    }
    agent_handoffs = list(state.get("agent_handoffs", [])) + [handoff]

    writer({"type": "handoff_result", "from": "searchAgent", "to": "planner", "result": result.get("summary", "")})

    return {
        "research_notes": research_notes,
        "sources": sources,
        "agent_handoffs": agent_handoffs,
        "search_agent_summary": result.get("summary", ""),
        "last_actor_summary": result.get("summary", ""),
        "messages": [AIMessage(content=f"[searchAgent] {result.get('summary', '')}")],
        "context_next_node": "context_monitor",
    }


def code_agent_node(state: MokioGraphState) -> dict[str, Any]:
    """代码智能体节点（图上的独立节点）

    职责：
    1. 读取 planner 的路由指令
    2. 执行 codeAgent
    3. 更新 todos / code_agent_summary / agent_handoffs

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：todos, code_agent_summary, agent_handoffs, messages
    """
    writer = _get_writer()
    instruction = state.get("planner_route_instruction", "") or state.get("task", "")
    writer({"type": "handoff", "from": "planner", "to": "codeAgent", "instruction": instruction})
    result = run_code_agent(state, instruction, writer=writer)

    todos = result.get("todos", state.get("todos", []))
    code_agent_summary = result.get("summary", "")
    handoff = {
        "from_agent": "planner",
        "to_agent": "codeAgent",
        "instruction": instruction,
        "result": code_agent_summary,
    }
    agent_handoffs = list(state.get("agent_handoffs", [])) + [handoff]

    writer({"type": "handoff_result", "from": "codeAgent", "to": "planner", "result": code_agent_summary})

    return {
        "todos": todos,
        "code_agent_summary": code_agent_summary,
        "last_actor_summary": code_agent_summary,
        "agent_handoffs": agent_handoffs,
        "messages": [AIMessage(content=f"[codeAgent] {code_agent_summary}")],
        "context_next_node": "context_monitor",
    }


def repair_node(state: MokioGraphState) -> dict[str, Any]:
    """修复节点

    职责：
    1. 读取 verifier 的修复建议（repair_instruction）
    2. 委派 codeAgent 执行修复
    3. 更新 todos / code_agent_summary / agent_handoffs

    Args:
        state: 当前工作流状态

    Returns:
        更新的状态字段：todos, code_agent_summary, agent_handoffs, messages
    """
    writer = _get_writer()
    instruction = state.get("repair_instruction", "") or state.get("last_error", "")
    if not instruction:
        instruction = "Inspect the workspace and complete the unfinished task."

    writer({"type": "handoff", "from": "verifier", "to": "codeAgent", "instruction": instruction})
    result = run_code_agent(state, instruction, writer=writer)

    todos = result.get("todos", state.get("todos", []))
    code_agent_summary = result.get("summary", "")
    handoff = {
        "from_agent": "verifier",
        "to_agent": "codeAgent",
        "instruction": instruction,
        "result": code_agent_summary,
    }
    agent_handoffs = list(state.get("agent_handoffs", [])) + [handoff]

    writer({"type": "handoff_result", "from": "codeAgent", "to": "verifier", "result": code_agent_summary})

    return {
        "todos": todos,
        "code_agent_summary": code_agent_summary,
        "last_actor_summary": code_agent_summary,
        "agent_handoffs": agent_handoffs,
        "repair_instruction": "",
        "messages": [AIMessage(content=f"[repair→codeAgent] {code_agent_summary}")],
        "context_next_node": "verifier",
    }


def verifier_route(state: MokioGraphState) -> str:
    """校验器路由函数

    根据校验结果决定下一步：
    - final: 校验通过或达到最大重试次数
    - repair: 校验失败，有明确的修复建议
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
    repair_instruction = state.get("repair_instruction", "")
    if repair_instruction:
        return "repair"
    return "planner"


def final_node(state: MokioGraphState) -> dict[str, Any]:
    """结束节点

    生成更紧凑、可操作的最终结果摘要。
    """
    status = "PASSED" if state.get("passed") else "FAILED"
    verdict = _final_verdict_line(state, status)
    summary = _final_summary_block(state)
    next_step = _final_next_step(state)
    final_answer = "\n\n".join(part for part in [verdict, summary, next_step] if part)
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
        # CJK-aware fallback: Chinese chars ~1.5 tokens, ASCII ~0.25 tokens
        cjk_count = sum(1 for ch in text if '一' <= ch <= '鿿')
        ascii_count = len(text) - cjk_count
        return max(1, int(cjk_count * 1.5 + ascii_count * 0.25))


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
    runtime = state["runtime"]
    tool_message = execute_tool_by_name(
        _build_planner_tools(state, writer), call,
        hook_runner=runtime.hook_runner,
        budget=runtime.result_budget,
        workspace=runtime.workspace,
        runtime=runtime,
    )
    writer(build_tool_result_event(tool_message, node="planner"))
    return tool_message


def _execute_read_only_tool(state: MokioGraphState, call: dict[str, Any]) -> ToolMessage:
    """执行只读工具调用（传入 runtime 以启用 agent_mode 门禁）"""
    runtime = state["runtime"]
    return execute_tool_by_name(
        build_read_only_tools(runtime), call,
        hook_runner=runtime.hook_runner,
        budget=runtime.result_budget,
        workspace=runtime.workspace,
        runtime=runtime,
    )


def _compress_context_with_model(state: MokioGraphState) -> dict[str, Any]:
    builder = _get_prompt_builder(state)
    memory = build_layered_memory(state, node="context_compressor")
    payload = {
        "context_summary": state.get("context_summary", ""),
        "memory": memory,
        "messages": [_message_snapshot(message) for message in state.get("messages", [])],
    }
    messages = [
        SystemMessage(content=builder.build("context_compressor")),
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
    active_goal = str(state.get("task", ""))
    continuation = "Continue the current plan."
    if state.get("verifier_summary") and not state.get("passed"):
        continuation = f"Resume the repair loop: verify and fix remaining issues."
    elif state.get("plan_summary"):
        continuation = f"Continue the current plan: {state.get('plan_summary', '')[:300]}"
    elif active_goal:
        continuation = f"Continue the original task: {active_goal[:300]}"
    payload = {
        "type": "mokio_context_summary",
        "task": active_goal,
        "active_goal": active_goal,
        "continuation_hint": continuation,
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "attempts": state.get("attempts", 0),
        "passed": state.get("passed"),
        "verifier_summary": state.get("verifier_summary", ""),
        "repair_instruction": state.get("repair_instruction", ""),
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
        f"Task: {sanitize_user_input(state['task'])}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn coding session:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)


def _verifier_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [f"Task: {sanitize_user_input(state['task'])}"]
    if state.get("plan_summary"):
        parts.append("Plan summary:\n" + str(state.get("plan_summary", "")))
    if state.get("todos"):
        parts.append("Todos:\n" + json.dumps(state.get("todos", []), ensure_ascii=False, default=str))
    if state.get("acceptance_criteria"):
        criteria = "\n".join(f"- {item}" for item in state.get("acceptance_criteria", []) or [])
        parts.append("Acceptance criteria that must all be checked:\n" + criteria)
    if state.get("verification_commands"):
        commands = "\n".join(f"- {cmd}" for cmd in state.get("verification_commands", []) or [])
        parts.append("Relevant verification commands:\n" + commands)
    if state.get("last_actor_summary"):
        parts.append("Latest actor summary:\n" + str(state.get("last_actor_summary", "")))
    if state.get("session_context"):
        parts.append("Session context for this multi-turn coding session:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    parts.append(
        "Inspect the workspace with tools and return only verifier JSON. "
        "Include one check for every acceptance criterion and relevant verification command. "
        "Do not pass the task unless all listed criteria are concretely satisfied."
    )
    return "\n\n".join(parts)


def _router_input(state: MokioGraphState) -> str:
    """构建路由器/聊天输入提示词"""
    parts = [f"User input:\n{sanitize_user_input(state.get('task', ''))}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


_chat_input = _router_input


def _write_plan_todo_file(runtime: Any, state: MokioGraphState) -> Any:
    """plan 模式：把计划落到 id_todo.md 供用户确认"""
    from pathlib import Path

    path = Path(runtime.workspace) / "id_todo.md"
    lines = [
        "# Plan (agent_mode=plan)",
        "",
        f"## Task\n{state.get('task', '')}",
        "",
        f"## Summary\n{state.get('plan_summary', '')}",
        "",
        "## Todos",
    ]
    for todo in state.get("todos", []) or []:
        status = todo.get("status", "pending")
        lines.append(f"- [{ 'x' if status == 'completed' else ' ' }] **{todo.get('id', '')}**: {todo.get('content', '')}")
    lines.extend(["", "## Acceptance Criteria"])
    for item in state.get("acceptance_criteria", []) or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Verification Commands"])
    for cmd in state.get("verification_commands", []) or []:
        lines.append(f"- `{cmd}`")
    lines.extend(
        [
            "",
            "## Next",
            "Review this plan, then run `/mode auto` or `/mode approve` and continue the task.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        runtime.record_read(path, complete=True)
    except Exception:
        pass
    return path


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
    # 优先匹配 fenced code block 中的 JSON（非贪婪，避免跨块匹配）
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


def _merge_acceptance_gate_checks(state: MokioGraphState, checks: list[VerificationCheck]) -> list[VerificationCheck]:
    merged = list(checks)
    if state.get("acceptance_criteria") and not merged:
        merged.append(
            {
                "name": "acceptance_criteria_checked",
                "passed": False,
                "detail": "Verifier returned no concrete checks for the acceptance criteria.",
            }
        )
    if state.get("verification_commands"):
        command_checks = [check for check in merged if "command" in check.get("name", "").lower()]
        if not command_checks:
            merged.append(
                {
                    "name": "verification_commands_checked",
                    "passed": False,
                    "detail": "Verifier did not report whether the requested verification commands were run or justified as irrelevant.",
                }
            )
    return merged


def _checks_passed(checks: list[VerificationCheck]) -> bool:
    return bool(checks) and all(bool(check.get("passed")) for check in checks)


def _recommended_from_failed_checks(checks: list[VerificationCheck]) -> str:
    failed = [check for check in checks if not check.get("passed")]
    if not failed:
        return "Inspect the workspace and complete any missing acceptance criteria."
    details = "; ".join(f"{check.get('name', 'check')}: {check.get('detail', '')}" for check in failed[:3])
    return f"Fix the failed verification checks, then rerun verification: {details}"


def _summarize_checks(checks: list[VerificationCheck], passed: bool) -> str:
    if not checks:
        return "Verifier returned no checks."
    failed = [check for check in checks if not check.get("passed")]
    if passed:
        return f"All {len(checks)} verification check(s) passed."
    return f"{len(failed)} of {len(checks)} verification check(s) failed."


def _format_verifier_error(reason: str, recommended: str, tool_events: list[dict[str, Any]]) -> str:
    event_text = json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )


def _final_verdict_line(state: MokioGraphState, status: str) -> str:
    attempts = state.get("attempts", 0)
    return f"{status}: {state.get('task', '').strip() or 'Task completed'} ({attempts} attempt(s))"


def _final_summary_block(state: MokioGraphState) -> str:
    parts = []
    plan = str(state.get("plan_summary", "")).strip()
    if plan:
        parts.append(f"Plan: {plan}")
    todos = _format_todo_summary(state.get("todos", []))
    if todos:
        parts.append(f"Todos: {todos}")
    verifier = str(state.get("verifier_summary", "")).strip()
    if verifier:
        parts.append(f"Verifier: {verifier}")
    checks = _format_check_summary(state.get("verification_checks", []))
    if checks:
        parts.append(f"Checks: {checks}")
    sources = _format_source_summary(state.get("sources", []))
    if sources:
        parts.append(f"Sources: {sources}")
    compression = _format_compression_summary(state.get("compression_events", []))
    if compression:
        parts.append(f"Context: {compression}")
    actor = str(state.get("code_agent_summary") or state.get("last_actor_summary", "")).strip()
    if actor:
        parts.append(f"Implementation: {actor}")
    return "\n".join(parts)


def _final_next_step(state: MokioGraphState) -> str:
    if state.get("passed"):
        return "Next: review the changes, then run a quick verification if you want extra confidence."
    recommendation = str(state.get("repair_instruction") or state.get("last_error") or "").strip()
    if recommendation:
        return f"Next: {recommendation}"
    return "Next: inspect the failed checks and repair the workspace."


def _format_todo_summary(todos: list[dict[str, Any]]) -> str:
    items = []
    for todo in todos[:4]:
        status = todo.get("status", "")
        content = str(todo.get("content", "")).strip()
        if content:
            items.append(f"{status}: {content}")
    if len(todos) > 4:
        items.append(f"+{len(todos) - 4} more")
    return "; ".join(items)


def _format_check_summary(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "(none)"
    passed = sum(1 for check in checks if check.get("passed"))
    total = len(checks)
    return f"{passed}/{total} passed"


def _format_source_summary(sources: list[dict[str, Any]]) -> str:
    items = []
    for source in sources[:3]:
        title = str(source.get("title", "")).strip()
        url = str(source.get("url", "")).strip()
        if title and url:
            items.append(f"{title} ({url})")
        elif url:
            items.append(url)
    if len(sources) > 3:
        items.append(f"+{len(sources) - 3} more")
    return "; ".join(items)


def _format_compression_summary(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(none)"
    latest = events[-1]
    return (
        f"{len(events)} compression(s), "
        f"latest {latest.get('before_tokens')} -> {latest.get('after_tokens')} tokens, "
        f"removed {latest.get('removed_messages')} message(s)"
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


def _persist_raw_history(runtime: Any, messages: list[Any]) -> None:
    """持久化完整对话历史（不发送给模型，仅用于审计和摘要重建）

    面试官考察点：
    - "前10轮原始上下文就不需要了吗" → 需要！持久化但不发送
    - 用途：审计溯源、摘要重建、长期记忆检索

    Args:
        runtime: 运行时状态
        messages: 完整消息列表
    """
    try:
        from datetime import datetime

        workspace = runtime.workspace
        history_file = workspace / "RAW_HISTORY.md"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        # 格式化消息
        lines = [f"# Raw Conversation History\n", f"_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n"]
        for msg in messages:
            msg_type = type(msg).__name__
            content = str(msg.content or "")
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                lines.append(f"## {msg_type} (with {len(msg.tool_calls)} tool calls)")
            else:
                lines.append(f"## {msg_type}")
            lines.append(content[:500])  # 截断避免文件过大
            lines.append("")

        # 追加到文件（保留历史）
        with open(history_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n---\n\n")

    except Exception as exc:
        logger.warning("Failed to persist raw history: %s", exc)


_last_ai_content = last_ai_content


def _get_writer():
    try:
        langgraph_writer = get_stream_writer()
    except RuntimeError:
        langgraph_writer = None

    bus = get_event_bus()

    def writer(event: dict[str, Any]) -> None:
        if langgraph_writer is not None:
            try:
                langgraph_writer(event)
            except Exception as exc:
                logger.debug("stream_writer error: %s", exc)
        try:
            bus.emit(event)
        except Exception as exc:
            logger.debug("event_bus emit error: %s", exc)

    return writer
