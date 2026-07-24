import re

from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage
from langgraph.graph import StateGraph
from langgraph.graph import END, START
from typing import Any
import json

from langgraph.graph.message import REMOVE_ALL_MESSAGES

from core.node import planner_node, context_monitor_node, verifier_node, intent_router_node, chat_responder_node, \
    intent_route_fn, _get_writer, estimate_context_tokens, _extract_json, _message_text
from core.state import DotAgentGraphState
from memory.memory import build_layered_memory, memory_event, _short_text, persist_history_summary, _trim_handoffs
from model.openai_provider import create_model
from prompt.prompt import CONTEXT_COMPRESSION_PROMPT


def build_complex_workflow():
    graph = StateGraph(DotAgentGraphState)
    graph.add_node("planner", planner_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node("context_compressor", context_compressor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "context_monitor")
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {"context_compressor": "context_compressor", "verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_conditional_edges(
        "context_compressor",
        context_compressor_route,
        {"verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_edge("verifier", "context_monitor")
    graph.add_edge("final", END)
    return graph.compile()


def build_entry_workflow():
    graph = StateGraph(DotAgentGraphState)
    graph.add_node("intent_router", intent_router_node)
    graph.add_node("chat_responder", chat_responder_node)

    graph.add_edge(START, "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        intent_route_fn,
        {"chat_responder": "chat_responder", "planner": END},
    )
    graph.add_edge("chat_responder", END)
    return graph.compile()


def context_monitor_route(state: DotAgentGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: DotAgentGraphState) -> dict[str, Any]:
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))
    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))
    compressed = _compress_context_with_model(state)
    summary = _format_compressed_context(compressed, state)
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    post_state: DotAgentGraphState = {
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


def _compress_context_with_model(state: DotAgentGraphState) -> dict[str, Any]:
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
        return _fallback_compression(state, error=f"{type(exc).__name__}: {exc}")
    return _fallback_compression(state, error="compressor model did not return valid JSON")


def context_compressor_route(state: DotAgentGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: DotAgentGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: DotAgentGraphState) -> dict[str, Any]:
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


def _message_snapshot(message: Any) -> dict[str, str]:
    return {
        "type": type(message).__name__,
        "name": str(getattr(message, "name", "") or ""),
        "content": _short_text(_message_text(message), 2000),
    }


def _fallback_compression(state: DotAgentGraphState, *, error: str = "") -> dict[str, Any]:
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
        "sources": [{"title": source.get("title", ""), "url": source.get("url", "")} for source in
                    state.get("sources", [])],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _format_compressed_context(compressed: dict[str, Any], state: DotAgentGraphState) -> str:
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


def _important_files_from_state(state: DotAgentGraphState) -> list[str]:
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
