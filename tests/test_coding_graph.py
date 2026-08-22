"""Tests for the Coding Agent LangGraph skeleton."""
from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mokioclaw.orchestration.coding_graph import (
    REPLAN_THRESHOLD,
    MAX_ATTEMPT_DEFAULT,
    CodingAgentState,
    build_graph,
    build_initial_state,
    context_compress_node,
    finally_node,
    get_session_manager,
    human_intervene_marker_node,
    plan_node,
    route_coding_agent,
    route_human_intervene,
    route_valid_node,
    SessionManager,
    valid_node,
)


# ============================================================
# Helpers
# ============================================================

@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path


def _basic_state(**overrides) -> CodingAgentState:
    state: CodingAgentState = {
        "messages": [HumanMessage(content="Build a web server")],
        "task_plan": {},
        "replan_count": 0,
        "attempt_count": 0,
        "max_attempt": MAX_ATTEMPT_DEFAULT,
        "validate_result": {},
        "tool_artifacts": {},
        "need_human_intervene": False,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


# ============================================================
# build_initial_state
# ============================================================

def test_build_initial_state_defaults():
    state = build_initial_state()
    assert state["replan_count"] == 0
    assert state["attempt_count"] == 0
    assert state["max_attempt"] == MAX_ATTEMPT_DEFAULT
    assert state["need_human_intervene"] is False
    assert state["messages"] == []
    assert state["tool_artifacts"] == {}
    assert state["task_plan"] == {}
    assert state["validate_result"] == {}


def test_build_initial_state_with_messages():
    msgs = [HumanMessage(content="hello")]
    state = build_initial_state(messages=msgs)
    assert state["messages"] == msgs


def test_build_initial_state_custom_thresholds():
    state = build_initial_state(replan_max=5, max_attempt=10)
    assert state["max_attempt"] == 10


# ============================================================
# Nodes (stubs)
# ============================================================

class TestNodesReturnExpectedKeys:
    def test_context_compress_node_returns_empty(self):
        result = context_compress_node(_basic_state())
        assert result == {}

    def test_plan_node_returns_task_plan(self):
        result = plan_node(_basic_state())
        assert "task_plan" in result

    def test_coding_agent_node_returns_messages(self):
        result = _find_node("coding_agent_node")(_basic_state())
        assert "messages" in result

    def test_valid_node_returns_validate_result(self):
        result = valid_node(_basic_state())
        assert "validate_result" in result
        # 无 validation_commands 时 all_passed 保持 True（空列表 vacuously true）
        assert "passed" in result["validate_result"]

    def test_human_intervene_marker_sets_flag(self):
        result = human_intervene_marker_node(_basic_state())
        assert result["need_human_intervene"] is True

    def test_finally_node_returns_final_answer(self):
        result = finally_node(_basic_state())
        assert "final_answer" in result


def _find_node(name: str):
    """Import node by name from module."""
    import mokioclaw.orchestration.coding_graph as mod
    return getattr(mod, name)


# ============================================================
# Routers
# ============================================================

class TestRouteCodingAgent:
    def test_plan_invalid_replan_under_threshold_goes_to_plan(self):
        state = _basic_state(
            tool_artifacts={"plan_invalid": True},
            replan_count=0,
        )
        assert route_coding_agent(state) == "plan_node"

    def test_plan_invalid_replan_at_threshold_goes_to_human(self):
        state = _basic_state(
            tool_artifacts={"plan_invalid": True},
            replan_count=REPLAN_THRESHOLD,
        )
        assert route_coding_agent(state) == "human_intervene_marker"

    def test_plan_valid_goes_to_valid_node(self):
        state = _basic_state(tool_artifacts={})
        assert route_coding_agent(state) == "valid_node"

    def test_plan_invalid_but_no_flag_goes_to_valid(self):
        state = _basic_state(tool_artifacts={"plan_invalid": False})
        assert route_coding_agent(state) == "valid_node"


class TestRouteValidNode:
    def test_passed_goes_to_finally(self):
        state = _basic_state(validate_result={"passed": True})
        assert route_valid_node(state) == "finally_node"

    def test_failed_under_max_goes_to_coding(self):
        state = _basic_state(
            validate_result={"passed": False},
            attempt_count=1,
            max_attempt=3,
        )
        assert route_valid_node(state) == "coding_agent_node"

    def test_failed_at_max_goes_to_human(self):
        state = _basic_state(
            validate_result={"passed": False},
            attempt_count=3,
            max_attempt=3,
        )
        assert route_valid_node(state) == "human_intervene_marker"

    def test_failed_at_max_custom_goes_to_human(self):
        state = _basic_state(
            validate_result={"passed": False},
            attempt_count=5,
            max_attempt=5,
        )
        assert route_valid_node(state) == "human_intervene_marker"


class TestRouteHumanIntervene:
    def test_continue_goes_to_plan(self):
        assert route_human_intervene({"resume_action": "continue"}) == "plan_node"

    def test_stop_goes_to_finally(self):
        assert route_human_intervene({"resume_action": "stop"}) == "finally_node"

    def test_empty_goes_to_finally(self):
        assert route_human_intervene({}) == "finally_node"


# ============================================================
# build_graph
# ============================================================

def test_build_graph_compiles():
    compiled = build_graph().compile()
    assert compiled is not None


# ============================================================
# SessionManager
# ============================================================

def test_session_manager_create_and_get():
    mgr = SessionManager()
    session = mgr.create_session("s1")
    assert session.session_id == "s1"
    assert session.compiled_graph is not None
    # _init_session 从磁盘恢复，workspace 被设为实际路径，不再等于无参 build_initial_state()
    assert session.current_state["task"] == ""
    assert session.current_state["task_plan"] == {}
    assert session.current_state["replan_count"] == 0
    assert session.current_state["attempt_count"] == 0
    assert session.current_state["max_attempt"] == MAX_ATTEMPT_DEFAULT
    assert session.current_state["need_human_intervene"] is False
    assert session.is_running is False
    assert session.replan_max == REPLAN_THRESHOLD
    assert session.max_attempt == MAX_ATTEMPT_DEFAULT

    fetched = mgr.get_session("s1")
    assert fetched is session


def test_session_manager_create_auto_id():
    mgr = SessionManager()
    session = mgr.create_session()
    assert session.session_id  # non-empty uuid


def test_session_manager_duplicate_returns_existing():
    """create_session 幂等：重复调用同一 ID 返回已有 session（支持 warmup 复用）"""
    mgr = SessionManager()
    s1 = mgr.create_session("s1")
    s2 = mgr.create_session("s1")
    assert s1 is s2


def test_session_manager_get_missing_auto_creates():
    mgr = SessionManager()
    # 不存在的 session 自动创建
    sess = mgr.get_session("nonexistent")
    assert sess.session_id == "nonexistent"
    assert sess.compiled_graph is not None


def test_session_manager_destroy_then_get_auto_creates():
    mgr = SessionManager()
    mgr.create_session("s1")
    mgr.destroy_session("s1")
    # 销毁后再次 get，自动创建新 session
    sess = mgr.get_session("s1")
    assert sess.session_id == "s1"


def test_session_manager_disk_recovery(tmp_path):
    """_init_session 从磁盘恢复 messages 和计数"""
    from mokioclaw.orchestration.session_persistence import SessionPersistence
    from langchain_core.messages import HumanMessage

    root = tmp_path / "sessions"
    root.mkdir()
    sid = "disk-session"

    # 先在磁盘上写一个 session
    persistence = SessionPersistence(sessions_root=root)
    meta = persistence._empty_session_meta(sid)
    meta["messages"] = [{"type": "HumanMessage", "content": "hello from disk"}]
    meta["replan_count"] = 2
    meta["attempt_count"] = 1
    meta["current_turn_id"] = 3
    persistence.save_session_meta(sid, meta)

    # 创建 SessionManager 并获取该 session
    mgr = SessionManager(sessions_root=root)
    session = mgr.get_session(sid)

    assert session.session_id == sid
    assert session.current_turn_id == 3
    assert len(session.current_state["messages"]) == 1
    assert session.current_state["messages"][0].content == "hello from disk"
    assert session.current_state["replan_count"] == 2
    assert session.current_state["attempt_count"] == 1


def test_session_manager_destroy_idempotent():
    mgr = SessionManager()
    mgr.destroy_session("never-existed")  # should not raise


def test_stream_session_events_yields_and_resets_lock():
    mgr = SessionManager()
    session = mgr.create_session("s1")

    events = list(mgr.stream_session_events("s1"))
    # Stub nodes return empty dicts, stream should yield something
    assert session.is_running is False  # finally reset

    # Concurrent call should raise
    with pytest.raises(RuntimeError, match="already running"):
        list(mgr.stream_session_events("s1"))


def test_stream_session_events_appends_user_input():
    mgr = SessionManager()
    session = mgr.create_session("s1")
    list(mgr.stream_session_events("s1", new_user_input="hello"))
    assert len(session.current_state["messages"]) == 1
    assert session.current_state["messages"][0].content == "hello"


# ============================================================
# get_session_manager singleton
# ============================================================

def test_get_session_manager_singleton():
    import mokioclaw.orchestration.coding_graph as mod
    mod._session_manager = None
    a = get_session_manager()
    b = get_session_manager()
    assert a is b


# ============================================================
# Constants
# ============================================================

def test_constants():
    assert REPLAN_THRESHOLD == 3
    assert MAX_ATTEMPT_DEFAULT == 3
