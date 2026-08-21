from __future__ import annotations

import json
from pathlib import Path

from mokioclaw.state.runtime import RuntimeState
from mokioclaw.reliability.trace import TraceRecorder


def test_trace_recorder_writes_events_summary_and_timeline(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    trace.start({"task": "demo", "max_attempts": 3})
    trace.record_graph_update({"planner": {"plan_summary": "plan"}})
    trace.record_custom_event({"type": "tool_call", "node": "codeAgent", "name": "BashTool", "args": {"command": "python --version"}})
    trace.record_custom_event({"type": "tool_result", "node": "codeAgent", "name": "BashTool", "result": {"ok": True}})
    trace.end(status="finished", latest_node="final", final_state={"passed": True})
    summary = trace.summary_payload()

    assert summary["trace_id"] == trace.trace_id
    assert (trace.root / "events.jsonl").exists()
    summary = summary
    assert summary["node_visits"] == {"planner": 1}
    assert summary["tool_calls"] == 1


def test_trace_recorder_trims_long_payload(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")
    long_text = "x" * 5000

    trace.start({"task": "demo"})
    trace.record_custom_event({"type": "tool_result", "result": {"ok": True, "stdout": long_text}})

    line = (trace.root / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)["payload"]
    assert len(payload["payload"]["result"]["stdout"]) < 1300
    assert long_text not in line


def test_trace_recorder_counts_failed_tools_and_approvals(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    trace.record_custom_event({"type": "tool_call", "node": "codeAgent", "name": "BashTool"})
    trace.record_custom_event(
        {
            "type": "tool_result",
            "node": "codeAgent",
            "name": "BashTool",
            "result": {"ok": False, "requires_approval": True, "approved": False},
        }
    )
    trace.end(status="interrupted", latest_node="planner")
    summary = trace.summary_payload()

    assert summary["tool_calls"] == 1
    assert summary["failed_tool_calls"] == 1
    assert summary["approval_count"] == 1


def test_trace_recorder_off_mode_does_not_create_files(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="off")
    trace = TraceRecorder(runtime, task="demo")

    trace.start({"task": "demo"})
    event = trace.end(status="finished")

    assert event is None
    assert not (tmp_path / ".mokioclaw" / "executions").exists()


def test_trace_recorder_write_errors_do_not_raise(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", broken_open)
    trace.record_custom_event({"type": "tool_call", "name": "BashTool"})

    assert trace.errors


def test_trace_140_events_all_recorded(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    for idx in range(140):
        trace.record("custom:test", {"event_type": f"event-{idx}"})
    trace.end(status="finished")
    summary = trace.summary_payload()
    assert summary["event_count"] == 141  # 140 custom + 1 run_end


def test_trace_summary_includes_final_state_summary(tmp_path: Path) -> None:
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="trace-demo")
    trace.end(
        status="finished",
        latest_node="final",
        final_state={
            "passed": True,
            "attempts": 2,
            "plan_summary": "refine outputs",
            "verifier_summary": "looks good",
            "repair_instruction": "tighten memory",
            "acceptance_criteria": ["a"],
            "verification_checks": ["b"],
        },
    )

    summary = trace.summary_payload()

    assert "refine outputs" in summary["summary"]
    assert summary["final_state"]["attempts"] == 2


def test_trace_summary_field_order(tmp_path: Path) -> None:
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="demo")
    trace.end(status="finished", latest_node="final", final_state={"passed": True, "attempts": 1, "plan_summary": "plan"})

    summary = trace.summary_payload()

    keys = list(summary.keys())
    assert keys[:4] == ["trace_id", "status", "final_status", "summary"]
