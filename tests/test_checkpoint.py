from __future__ import annotations

import json
import shutil
from pathlib import Path

from langchain_core.messages import AIMessage

from mokioclaw.reliability.checkpoint import (
    CHECKPOINT_ROOT,
    CheckpointManager,
    build_light_resume_inputs,
    list_checkpoints,
    load_resume_inputs,
    normalize_resume_task,
    rollback_to_checkpoint,
    serialize_state,
    deserialize_state,
    workspace_manifest,
)
from mokioclaw.reliability.session_store import build_resume_context
from mokioclaw.state.runtime import RuntimeState


def sample_state(runtime: RuntimeState) -> dict:
    return {
        "task": "build demo",
        "runtime": runtime,
        "messages": [AIMessage(content="hello")],
        "plan_summary": "demo plan",
        "todos": [{"id": "todo-1", "content": "write app", "status": "in_progress", "note": ""}],
        "acceptance_criteria": ["app exists"],
        "verification_commands": ["python --version"],
        "sources": [{"title": "Docs", "url": "https://example.com"}],
        "research_notes": "notes",
        "attempts": 1,
        "max_attempts": 3,
        "context_next_node": "verifier",
    }


def test_light_checkpoint_writes_recovery_and_checkpoint(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="light")
    (tmp_path / "TODO.md").write_text("todo content", encoding="utf-8")

    event = CheckpointManager(runtime, task="build demo").save(sample_state(runtime), status="running", latest_node="planner")

    root = tmp_path / CHECKPOINT_ROOT
    assert event is not None
    assert event["type"] == "checkpoint_saved"
    assert (root / "checkpoint.json").exists()
    assert (root / "RECOVERY.md").exists()
    recovery = (root / "RECOVERY.md").read_text(encoding="utf-8")
    assert "demo plan" in recovery
    assert "write app" in recovery
    assert not (root / "state.json").exists()


def test_strict_checkpoint_writes_state_and_events(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="strict")
    manager = CheckpointManager(runtime, task="build demo")

    manager.save(sample_state(runtime), status="running", latest_node="planner", event={"mode": "custom", "payload": {"type": "x"}})

    root = tmp_path / CHECKPOINT_ROOT
    assert (root / "state.json").exists()
    assert (root / "events.jsonl").exists()
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert "runtime" not in state
    assert state["messages"][0]["type"] == "ai"


def test_state_serialization_restores_messages_and_runtime(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="strict")
    payload = serialize_state(sample_state(runtime))

    restored = deserialize_state(payload, runtime)

    assert restored["runtime"] is runtime
    assert restored["messages"][0].content == "hello"
    assert restored["plan_summary"] == "demo plan"


def test_workspace_manifest_excludes_checkpoint_directory(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
    checkpoint_file = tmp_path / CHECKPOINT_ROOT / "checkpoint.json"
    checkpoint_file.parent.mkdir(parents=True)
    checkpoint_file.write_text("{}", encoding="utf-8")

    manifest = workspace_manifest(tmp_path)

    assert [item["path"] for item in manifest] == ["app.py"]


def test_light_checkpoint_git_snapshot_handles_relative_workspace(tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:
        return
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hi')", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runtime = RuntimeState(workspace=Path("workspace"), checkpoint_mode="light")

    event = CheckpointManager(runtime, task="relative workspace").save(sample_state(runtime), status="running", latest_node="planner")

    assert event is not None
    assert event["git_error"] in {None, ""}
    assert event["git_commit"]


def test_light_resume_context_includes_workspace_memory(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="light")
    (tmp_path / "TODO.md").write_text("TODO from disk", encoding="utf-8")
    (tmp_path / "HISTORY_SUMMARY.md").write_text("History summary", encoding="utf-8")
    CheckpointManager(runtime, task="original task").save(sample_state(runtime), status="interrupted", latest_node="verifier")

    inputs = build_light_resume_inputs(runtime, max_attempts=5)

    assert "Continue this MokioClaw task" in inputs["task"]
    assert "TODO from disk" in inputs["context_summary"]
    assert "History summary" in inputs["context_summary"]
    assert inputs["plan_summary"] == "demo plan"
    assert inputs["max_attempts"] == 5


def test_light_resume_normalizes_repeated_resume_prefix(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="light")
    repeated = (
        "Continue this MokioClaw task from the checkpoint: "
        "Continue the interrupted MokioClaw task from the checkpoint: "
        "original task"
    )
    CheckpointManager(runtime, task=repeated).save({**sample_state(runtime), "task": repeated}, status="interrupted", latest_node="planner")

    inputs = build_light_resume_inputs(runtime)

    assert inputs["task"] == "Continue this MokioClaw task from the checkpoint: original task"


def test_normalize_resume_task_strips_nested_prefixes() -> None:
    assert normalize_resume_task(
        "Continue this MokioClaw task from the checkpoint: Continue this MokioClaw task from the checkpoint: demo"
    ) == "demo"


def test_strict_resume_falls_back_to_light_when_state_missing(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="strict")
    (tmp_path / "TODO.md").write_text("resume todo", encoding="utf-8")
    CheckpointManager(RuntimeState(workspace=tmp_path, checkpoint_mode="light"), task="original").save(
        sample_state(runtime),
        status="interrupted",
        latest_node="planner",
    )

    inputs, event = load_resume_inputs(runtime, max_attempts=2)

    assert event["type"] == "checkpoint_resumed"
    assert event["fallback"] is True
    assert event["mode"] == "light"
    assert "resume todo" in inputs["context_summary"]


def test_list_checkpoints_returns_metadata(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="light")
    manager = CheckpointManager(runtime, task="demo task")
    event1 = manager.save(
        sample_state(runtime),
        status="running",
        latest_node="planner",
    )
    # Update state for second checkpoint
    state2 = sample_state(runtime)
    state2["attempts"] = 2
    event2 = manager.save(
        state2,
        status="interrupted",
        latest_node="verifier",
    )

    checkpoints = list_checkpoints(tmp_path)

    # Each save overwrites the same checkpoint.json, so we get 1 checkpoint
    # with the latest state. This is expected behavior for light mode.
    assert len(checkpoints) >= 1
    latest = checkpoints[0].to_dict()
    assert latest["status"] == "interrupted"
    assert latest["latest_node"] == "verifier"
    assert latest["attempts"] == 2


def test_rollback_restores_checkpoint_payload(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode="light")
    manager = CheckpointManager(runtime, task="rollback demo")
    state = sample_state(runtime)
    state["task"] = "rollback demo"  # match manager task
    manager.save(state, status="interrupted", latest_node="verifier")

    checkpoints = list_checkpoints(tmp_path)
    assert len(checkpoints) == 1
    checkpoint_id = checkpoints[0].checkpoint_id

    payload = rollback_to_checkpoint(tmp_path, checkpoint_id, restore_workspace_files=False)

    assert payload["status"] == "interrupted"
    assert payload["latest_node"] == "verifier"
    assert payload["task"] == "rollback demo"


def test_rollback_raises_for_missing_checkpoint(tmp_path: Path) -> None:
    try:
        rollback_to_checkpoint(tmp_path, "checkpoint-nonexistent", restore_workspace_files=False)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_resume_context_includes_continuation_hint(tmp_path: Path) -> None:
    session = {
        "session_id": "session-1",
        "status": "running",
        "turn_index": 3,
        "task": "Fix parser and tests",
        "latest_checkpoint": "turn-003",
        "last_state_summary": {
            "attempts": 2,
            "plan_summary": "Refactor the loader chain",
            "repair_instruction": "Keep the config chain stable",
            "passed": False,
        },
        "turns": [],
    }

    context = build_resume_context(session)

    assert "continuation_hint" in context
    assert "Keep the config chain stable" in context
    assert "attempts=2" in context


def test_resume_context_prefers_repair_instruction(tmp_path: Path) -> None:
    session = {
        "session_id": "session-2",
        "status": "running",
        "turn_index": 1,
        "task": "Finish core parity",
        "last_state_summary": {
            "repair_instruction": "Fix tool gate semantics first",
        },
        "turns": [],
    }

    context = build_resume_context(session)

    assert "Fix tool gate semantics first" in context
