"""Tests for message persistence, rewind/resume, and progressive disclosure."""
from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mokioclaw.core.progressive_disclosure import parse_markers, resolve_skill
from mokioclaw.reliability.session_store import (
    append_messages_to_session,
    create_session,
    load_session_messages,
    load_turns_up_to,
    save_turn_checkpoint,
    save_session,
)
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.reliability.trace import TraceRecorder


# ============ Message Persistence ============


def test_session_creates_with_empty_messages(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    assert "messages" in session
    assert session["messages"] == []


def test_append_messages_to_session_persists_serialized(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="hi there"),
    ]
    append_messages_to_session(tmp_path, session, messages)

    # Reload from disk
    reloaded = json.loads((tmp_path / ".mokioclaw" / "sessions" / session["session_id"] / "session.json").read_text())
    assert len(reloaded["messages"]) == 2
    assert reloaded["messages"][0]["type"] == "human"
    assert reloaded["messages"][1]["type"] == "ai"
    assert reloaded["messages"][0]["data"]["content"] == "hello"


def test_append_messages_twice_accumulates(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    append_messages_to_session(tmp_path, session, [HumanMessage(content="turn 1")])
    append_messages_to_session(tmp_path, session, [AIMessage(content="turn 2")])

    reloaded = json.loads((tmp_path / ".mokioclaw" / "sessions" / session["session_id"] / "session.json").read_text())
    assert len(reloaded["messages"]) == 2
    assert reloaded["messages"][0]["data"]["content"] == "turn 1"
    assert reloaded["messages"][1]["data"]["content"] == "turn 2"


def test_save_turn_checkpoint_includes_messages_and_git_commit_id(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    messages = [HumanMessage(content="turn message")]
    checkpoint = save_turn_checkpoint(
        tmp_path, session, 1, "test task",
        turn_messages=messages,
    )

    turn_file = tmp_path / ".mokioclaw" / "sessions" / session["session_id"] / "turns" / "turn-001.json"
    assert turn_file.exists()
    data = json.loads(turn_file.read_text())
    assert data["turn"] == 1
    assert "git_commit_id" in data
    assert len(data["messages"]) == 1
    assert data["messages"][0]["type"] == "human"
    assert data["messages"][0]["data"]["content"] == "turn message"


def test_save_turn_checkpoint_without_messages(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    checkpoint = save_turn_checkpoint(tmp_path, session, 1, "test task")

    turn_file = tmp_path / ".mokioclaw" / "sessions" / session["session_id"] / "turns" / "turn-001.json"
    assert turn_file.exists()
    data = json.loads(turn_file.read_text())
    assert data["messages"] == []


# ============ load_turns_up_to ============


def test_load_turns_up_to_single_turn(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    msgs = [HumanMessage(content="q1"), AIMessage(content="a1")]
    save_turn_checkpoint(tmp_path, session, 1, "q1", turn_messages=msgs)

    loaded = load_turns_up_to(tmp_path, session["session_id"], 1)
    assert len(loaded) == 2
    assert loaded[0].content == "q1"
    assert loaded[1].content == "a1"


def test_load_turns_up_to_multiple_turns(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    save_turn_checkpoint(tmp_path, session, 1, "q1", turn_messages=[HumanMessage(content="q1")])
    save_turn_checkpoint(tmp_path, session, 2, "q2", turn_messages=[HumanMessage(content="q2"), AIMessage(content="a2")])

    loaded = load_turns_up_to(tmp_path, session["session_id"], 2)
    assert len(loaded) == 3
    assert loaded[0].content == "q1"
    assert loaded[1].content == "q2"
    assert loaded[2].content == "a2"


def test_load_turns_up_to_missing_turn_skips(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    save_turn_checkpoint(tmp_path, session, 1, "q1", turn_messages=[HumanMessage(content="q1")])
    # turn 2 doesn't exist

    loaded = load_turns_up_to(tmp_path, session["session_id"], 3)
    assert len(loaded) == 1


def test_load_turns_up_to_nonexistent_session(tmp_path: Path) -> None:
    loaded = load_turns_up_to(tmp_path, "session-nonexistent", 1)
    assert loaded == []


# ============ load_session_messages ============


def test_load_session_messages_empty(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    msgs = load_session_messages(tmp_path, session["session_id"])
    assert msgs == []


def test_load_session_messages_with_history(tmp_path: Path) -> None:
    session = create_session(tmp_path, "test task")
    append_messages_to_session(tmp_path, session, [
        HumanMessage(content="hello"),
        AIMessage(content="hi"),
    ])
    msgs = load_session_messages(tmp_path, session["session_id"])
    assert len(msgs) == 2
    assert msgs[0].content == "hello"
    assert msgs[1].content == "hi"


def test_load_session_messages_nonexistent(tmp_path: Path) -> None:
    msgs = load_session_messages(tmp_path, "session-nonexistent")
    assert msgs == []


# ============ Rollback truncates messages ============


def test_rollback_truncates_messages(tmp_path: Path) -> None:
    from mokioclaw.reliability.session_store import rollback_to_turn

    session = create_session(tmp_path, "test task")
    save_turn_checkpoint(tmp_path, session, 1, "q1", turn_messages=[HumanMessage(content="q1")])
    save_turn_checkpoint(tmp_path, session, 2, "q2", turn_messages=[HumanMessage(content="q2")])
    save_turn_checkpoint(tmp_path, session, 3, "q3", turn_messages=[HumanMessage(content="q3")])

    rollback_to_turn(tmp_path, session["session_id"], 2)

    reloaded = json.loads((tmp_path / ".mokioclaw" / "sessions" / session["session_id"] / "session.json").read_text())
    assert reloaded["turn_index"] == 2
    assert reloaded["latest_checkpoint"] == "turn-002"
    assert len(reloaded["messages"]) == 2  # only turns 1 and 2


# ============ Progressive Disclosure ============


def test_parse_markers_mcp() -> None:
    markers = parse_markers("I need to search the web. [NEED_MCP: web_search] Thanks!")
    assert len(markers) == 1
    assert markers[0]["type"] == "mcp"
    assert markers[0]["name"] == "web_search"


def test_parse_markers_skill() -> None:
    markers = parse_markers("Let me review this. [NEED_SKILL: code-review]")
    assert len(markers) == 1
    assert markers[0]["type"] == "skill"
    assert markers[0]["name"] == "code-review"


def test_parse_markers_multiple() -> None:
    text = "[NEED_MCP: search] and [NEED_SKILL: review] please"
    markers = parse_markers(text)
    assert len(markers) == 2
    assert markers[0]["type"] == "mcp"
    assert markers[1]["type"] == "skill"


def test_parse_markers_no_markers() -> None:
    markers = parse_markers("Just a normal response.")
    assert markers == []


def test_parse_markers_whitespace_variants() -> None:
    markers = parse_markers("[NEED_MCP:tool_without_space]")
    assert len(markers) == 1
    assert markers[0]["name"] == "tool_without_space"


def test_resolve_skill_loads_markdown(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".mokioclaw" / "skills"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# Test Skill\n\nThis is a test skill.", encoding="utf-8")

    body = resolve_skill("test-skill", tmp_path)
    assert body is not None
    assert "Test Skill" in body


def test_resolve_skill_not_found(tmp_path: Path) -> None:
    body = resolve_skill("nonexistent-skill", tmp_path)
    assert body is None


# ============ Hierarchical Trace ============


def test_trace_recorder_span_tracking(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="span test")

    trace.start({"task": "span test"})
    span1 = trace.start_span("planner")
    trace.end_span("planner")
    span2 = trace.start_span("code_agent")
    trace.end_span("code_agent")
    trace.end(status="finished", latest_node="final", final_state={"passed": True})

    summary = trace.summary_payload()
    assert len(summary["spans"]) == 2
    assert summary["spans"][0]["node"] == "planner"
    assert summary["spans"][0]["parent_span_id"] is None
    assert summary["spans"][1]["node"] == "code_agent"
    assert summary["spans"][1]["parent_span_id"] is None  # siblings, not parent-child


def test_trace_recorder_nested_spans(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="nested spans")

    trace.start({"task": "nested"})
    outer = trace.start_span("planner")
    inner = trace.start_span("tool_call")
    trace.end_span("tool_call")
    trace.end_span("planner")
    trace.end(status="finished", latest_node="final", final_state={})

    summary = trace.summary_payload()
    assert len(summary["spans"]) == 2
    assert summary["spans"][0]["parent_span_id"] is None  # outer
    assert summary["spans"][1]["parent_span_id"] == outer  # inner
    assert summary["spans"][1]["span_id"] == inner


def test_trace_recorder_graph_update_creates_spans(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="graph spans")

    trace.start({"task": "graph"})
    trace.record_graph_update({"planner": {"plan_summary": "plan"}})
    trace.record_graph_update({"code_agent": {"summary": "coded"}})
    trace.record_graph_update({"final": {"final_answer": "PASSED"}})
    trace.end(status="finished", latest_node="final", final_state={"passed": True})

    summary = trace.summary_payload()
    # planner and code_agent each get spans, final doesn't
    assert len(summary["spans"]) == 2
    nodes = [s["node"] for s in summary["spans"]]
    assert "planner" in nodes
    assert "code_agent" in nodes


def test_trace_recorder_off_mode_no_spans(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="off")
    trace = TraceRecorder(runtime, task="off")

    trace.start({"task": "off"})
    trace.start_span("planner")
    trace.end(status="finished", latest_node="final", final_state={})

    assert trace.summary_payload()["spans"] == []


# ============ PersistedState Expansion ============


def test_build_state_summary_includes_new_fields(tmp_path: Path) -> None:
    from mokioclaw.reliability.session_store import _build_state_summary

    state = {
        "plan_summary": "create a web app",
        "todos": [{"id": "1", "content": "setup", "status": "completed", "note": ""}],
        "acceptance_criteria": ["app runs"],
        "verification_commands": ["npm test"],
        "passed": True,
        "attempts": 2,
        "verifier_summary": "all tests pass",
        "repair_instruction": "",
        "last_error": "",
        "context_summary": "compressed context",
        "history_summary": "previous turns summary",
        "compression_events": [{"before_tokens": 1000, "after_tokens": 100, "removed_messages": 5, "summary": "compressed", "next_node": "verifier", "strategy": "hard"}],
        "research_notes": "found React docs",
        "sources": [{"title": "React", "url": "https://react.dev"}],
        "agent_handoffs": [{"from_agent": "planner", "to_agent": "codeAgent", "instruction": "build it", "result": "done"}],
        "code_agent_summary": "created app.py",
        "search_agent_summary": "searched docs",
        "last_actor_summary": "codeAgent",
        "verification_results": [{"command": "npm test", "ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}],
        "verification_checks": [{"name": "tests", "passed": True, "detail": "3/3"}],
    }

    summary = _build_state_summary(state)

    assert summary["plan_summary"] == "create a web app"
    assert summary["context_summary"] == "compressed context"
    assert summary["history_summary"] == "previous turns summary"
    assert len(summary["compression_events"]) == 1
    assert summary["research_notes"] == "found React docs"
    assert len(summary["sources"]) == 1
    assert "planner->codeAgent" in summary["agent_handoffs"]
    assert summary["code_agent_summary"] == "created app.py"
    assert summary["search_agent_summary"] == "searched docs"
    assert len(summary["verification_results"]) == 1
    assert len(summary["verification_checks"]) == 1
