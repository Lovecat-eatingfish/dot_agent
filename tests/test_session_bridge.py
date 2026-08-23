"""SessionBridge event normalization tests.

Drive run_turn with a fake host to verify:
  - message diff -> user/assistant/tool_call/tool_result events
  - chunk -> final/intervention/cancelled/node events
  - exception -> error event (no crash)
  - input history recording
"""
from __future__ import annotations

from typing import Any, Iterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from dot.cli.session_bridge import SessionBridge


class FakeSession:
    """Minimal session stub holding a messages list."""

    def __init__(self, messages: list | None = None) -> None:
        self.messages = messages or []
        self.cwd = None
        self.workspace = "/fake/ws"
        self.run_mode = "agent"
        self.is_running = False


class FakeHost:
    """AgentHost stub: run() yields controllable chunks."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.session = FakeSession()
        self._chunks = chunks or []
        self._run_mode = "agent"
        self._interrupted = False
        self.run_calls: list[str] = []

    def get_or_create_session(self, session_id: str | None = None) -> FakeSession:
        return self.session

    def get_session(self, session_id: str | None = None) -> FakeSession:
        return self.session

    def set_run_mode(self, mode: str, session_id: str | None = None) -> None:
        self._run_mode = mode
        self.session.run_mode = mode

    def get_run_mode(self, session_id: str | None = None) -> str:
        return self._run_mode

    def run(self, user_input: str, *, agent_mode: str = "auto") -> Iterator[dict[str, Any]]:
        self.run_calls.append(user_input)
        yield from self._chunks

    def has_pending_intervention(self, session_id: str | None = None) -> bool:
        return self._interrupted

    def request_cancel(self, session_id: str | None = None) -> bool:
        return True

    def resume_intervention(self, action: str, *, agent_mode: str = "auto") -> Iterator[dict[str, Any]]:
        yield from self._chunks


def _bridge(host: FakeHost) -> SessionBridge:
    return SessionBridge(host)


def test_human_message_becomes_user_event() -> None:
    bridge = _bridge(FakeHost())
    events = bridge._message_to_events(HumanMessage(content="hello"))

    assert len(events) == 1
    assert events[0]["kind"] == "user"
    assert events[0]["text"] == "hello"


def test_ai_message_with_tool_calls_and_content() -> None:
    bridge = _bridge(FakeHost())
    msg = AIMessage(
        content="let me check",
        tool_calls=[{"name": "bash", "args": {"command": "ls"}, "id": "1", "type": "tool_call"}],
    )
    events = bridge._message_to_events(msg)
    kinds = [e["kind"] for e in events]

    assert "tool_call" in kinds
    assert "assistant" in kinds
    tool_ev = next(e for e in events if e["kind"] == "tool_call")
    assert tool_ev["name"] == "bash"


def test_tool_message_becomes_tool_result() -> None:
    bridge = _bridge(FakeHost())
    events = bridge._message_to_events(ToolMessage(content="file list", name="bash", tool_call_id="1"))

    assert len(events) == 1
    assert events[0]["kind"] == "tool_result"
    assert events[0]["name"] == "bash"
    assert events[0]["content"] == "file list"


def test_system_message_is_hidden() -> None:
    bridge = _bridge(FakeHost())
    events = bridge._message_to_events(SystemMessage(content="you are a helper"))
    assert events == []


def test_final_chunk_yields_final_event() -> None:
    host = FakeHost(chunks=[{"finally_node": {"final_answer": "done!"}}])
    bridge = _bridge(host)

    events = list(bridge.run_turn("x"))
    final_ev = next(e for e in events if e["kind"] == "final")
    assert final_ev["answer"] == "done!"


def test_cancelled_chunk_yields_cancelled_event() -> None:
    host = FakeHost(chunks=[{"__dot_cancelled__": True}])
    bridge = _bridge(host)

    events = list(bridge.run_turn("x"))
    assert any(e["kind"] == "cancelled" for e in events)


def test_run_turn_always_ends_with_done() -> None:
    host = FakeHost()
    bridge = _bridge(host)

    events = list(bridge.run_turn("x"))
    assert events[-1]["kind"] == "done"


def test_run_turn_exception_yields_error_not_crash() -> None:
    host = FakeHost()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    host.run = boom
    bridge = _bridge(host)

    events = list(bridge.run_turn("x"))
    error_ev = next(e for e in events if e["kind"] == "error")
    assert "boom" in error_ev["text"]


def test_add_history_dedupes_adjacent() -> None:
    bridge = _bridge(FakeHost())
    bridge.add_history("hello")
    bridge.add_history("hello")
    bridge.add_history("world")

    assert bridge.history_prev() == "world"
    assert bridge.history_prev() == "hello"
    assert bridge.history_next() == "world"
    assert bridge.history_next() == ""


def test_add_history_ignores_blank() -> None:
    bridge = _bridge(FakeHost())
    bridge.add_history("   ")
    assert bridge.history_prev() is None


def test_cycle_mode_forward_and_back() -> None:
    bridge = _bridge(FakeHost())
    assert bridge.cycle_mode(forward=True) == "chat"
    assert bridge.cycle_mode(forward=True) == "code"
    assert bridge.cycle_mode(forward=False) == "chat"
