from __future__ import annotations

from pathlib import Path

from mokioclaw.interaction.formatter import (
    render_checkpoint_resumed,
    render_checkpoint_saved,
    render_context_compression,
    render_context_monitor,
    render_chat_response,
    render_intent_decision,
    render_memory_snapshot,
    render_plan,
    render_session_event,
    render_sources,
    render_trace_summary,
    render_final,
    render_verifier,
    print_custom_event,
)
from mokioclaw.state.runtime import RuntimeState


def test_render_plan_handles_todo_table(capsys) -> None:
    render_plan(
        {
            "plan_summary": "demo plan",
            "todos": [
                {"id": "todo-1", "content": "write tests", "status": "completed", "note": "done"},
                {"id": "todo-2", "content": "implement", "status": "in_progress", "note": ""},
            ],
            "verification_commands": ["python -m pytest -q"],
        },
        title="Test Plan",
    )

    output = capsys.readouterr().out
    assert "demo plan" in output
    assert "todo-1" in output
    assert "python -m pytest -q" in output


def test_render_sources_handles_source_table(capsys) -> None:
    render_sources(
        [{"title": "Amiya", "url": "https://example.com/amiya", "content": "Arknights character"}],
        title="searchAgent",
        answer="Amiya summary",
    )

    output = capsys.readouterr().out
    assert "Amiya summary" in output
    assert "https://example.com/amiya" in output


def test_render_verifier_handles_model_checks(capsys) -> None:
    render_verifier(
        {
            "passed": True,
            "attempts": 1,
            "verifier_summary": "looks good",
            "verification_checks": [{"name": "html", "passed": True, "detail": "file exists"}],
        }
    )

    output = capsys.readouterr().out
    assert "looks good" in output
    assert "html" in output


def test_render_context_monitor(capsys) -> None:
    render_context_monitor(
        {
            "context_token_count": 120,
            "context_token_limit": 100,
            "context_should_compress": True,
            "context_next_node": "verifier",
        }
    )

    output = capsys.readouterr().out
    assert "120" in output
    assert "verifier" in output


def test_render_chat_response(capsys) -> None:
    render_chat_response(
        {
            "type": "chat_response",
            "mode": "lightweight",
            "reason": "greeting",
            "response": "你好，我在。",
        }
    )

    output = capsys.readouterr().out
    assert "MokioClaw" in output
    assert "你好" in output


def test_render_intent_decision(capsys) -> None:
    render_intent_decision(
        {
            "type": "intent_decision",
            "route": "chat",
            "reason": "greeting",
            "confidence": 0.91,
        }
    )

    output = capsys.readouterr().out
    assert "Intent Router" in output
    assert "chat" in output
    assert "0.91" in output


def test_render_session_event(capsys) -> None:
    render_session_event(
        {
            "type": "session_started",
            "session_id": "session-demo",
            "workspace": "workspace-demo",
            "turn_index": 2,
            "resumed": False,
        }
    )

    output = capsys.readouterr().out
    assert "Session Started" in output
    assert "session-demo" in output


def test_print_custom_event_handles_session_saved(capsys) -> None:
    from mokioclaw.interaction.formatter import print_custom_event

    print_custom_event(
        {
            "type": "session_turn_saved",
            "turn": 1,
            "route": "chat",
            "summary_file": "SESSION_SUMMARY.md",
        }
    )

    output = capsys.readouterr().out
    assert "Session Saved" in output
    assert "chat" in output


def test_render_context_compression(capsys) -> None:
    render_context_compression(
        {
            "compression_events": [
                {
                    "before_tokens": 1000,
                    "after_tokens": 80,
                    "removed_messages": 12,
                    "next_node": "planner",
                    "summary": "compressed",
                }
            ]
        }
    )

    output = capsys.readouterr().out
    assert "1000" in output
    assert "compressed" in output


def test_render_memory_snapshot(capsys) -> None:
    render_memory_snapshot(
        {
            "node": "planner",
            "rules_count": 5,
            "todo_count": 2,
            "source_count": 1,
            "handoff_count": 1,
            "history_exists": False,
            "history_path": "HISTORY_SUMMARY.md",
            "layers": {
                "rules": "workspace rules",
                "working_memory": "task and todos",
                "history_summary_store": "compressed history",
            },
        }
    )

    output = capsys.readouterr().out
    assert "Memory Snapshot" in output
    assert "working_memory" in output
    assert "HISTORY_SUMMARY.md" in output


def test_print_custom_event_handles_memory_snapshot(capsys) -> None:
    from mokioclaw.interaction.formatter import print_custom_event

    print_custom_event(
        {
            "type": "memory_snapshot",
            "node": "verifier",
            "rules_count": 5,
            "todo_count": 1,
            "source_count": 0,
            "handoff_count": 0,
            "history_exists": True,
            "history_path": "HISTORY_SUMMARY.md",
            "layers": {
                "rules": "rules",
                "working_memory": "work",
                "history_summary_store": "history",
            },
        }
    )

    output = capsys.readouterr().out
    assert "Memory Snapshot" in output
    assert "verifier" in output


def test_render_checkpoint_saved(capsys) -> None:
    render_checkpoint_saved(
        {
            "mode": "light",
            "status": "interrupted",
            "path": ".mokioclaw/checkpoints",
            "checkpoint_file": "checkpoint.json",
            "recovery_file": "RECOVERY.md",
            "git_commit": "abc123",
            "resume_command": "uv run mokioclaw --resume ws",
        }
    )

    output = capsys.readouterr().out
    assert "Checkpoint Saved" in output
    assert "uv run mokioclaw --resume ws" in output


def test_render_checkpoint_resumed(capsys) -> None:
    render_checkpoint_resumed(
        {
            "mode": "strict",
            "workspace": "ws",
            "source": "state.json",
            "fallback": False,
        }
    )

    output = capsys.readouterr().out
    assert "Checkpoint Resumed" in output
    assert "strict" in output


def test_print_custom_event_handles_checkpoint_saved(capsys) -> None:
    from mokioclaw.interaction.formatter import print_custom_event

    print_custom_event(
        {
            "type": "checkpoint_saved",
            "mode": "light",
            "status": "finished",
            "path": ".mokioclaw/checkpoints",
            "resume_command": "uv run mokioclaw --resume ws",
        }
    )

    output = capsys.readouterr().out
    assert "Checkpoint Saved" in output


def test_render_trace_summary(capsys) -> None:
    render_trace_summary(
        {
            "trace_id": "trace-demo",
            "status": "finished",
            "duration_ms": 123,
            "trace_dir": ".mokioclaw/executions/trace-demo",
            "node_visits": {"planner": 1, "final": 1},
            "tool_calls": 2,
            "failed_tool_calls": 1,
            "approval_count": 1,
            "checkpoint_count": 3,
            "final_status": "passed",
        }
    )

    output = capsys.readouterr().out
    assert "Trace Summary" in output
    assert "trace-demo" in output
    assert "planner:1" in output


def test_render_final_event(capsys) -> None:
    render_final(
        {
            "type": "final",
            "final_answer": "PASSED\n\nPlan: demo\n\nNext: review changes",
        }
    )

    output = capsys.readouterr().out
    assert "Final" in output
    assert "PASSED" in output
    assert "Next: review changes" in output


def test_print_custom_event_handles_trace_summary(capsys) -> None:
    from mokioclaw.interaction.formatter import print_custom_event

    print_custom_event(
        {
            "type": "trace_summary",
            "trace_id": "trace-demo",
            "status": "interrupted",
            "duration_ms": 10,
            "trace_dir": ".mokioclaw/executions/trace-demo",
            "node_visits": {},
            "tool_calls": 0,
            "failed_tool_calls": 0,
            "approval_count": 0,
            "checkpoint_count": 1,
            "final_status": "",
            "summary": "demo summary",
        }
    )

    output = capsys.readouterr().out
    assert "Trace Summary" in output
    assert "interrupted" in output
    assert "summary:" in output


def test_render_resume_card(capsys, tmp_path) -> None:
    from mokioclaw.interaction.commands import _resume_command
    from mokioclaw.reliability.session_store import create_session, save_session

    session = create_session(tmp_path, "demo task")
    save_session(tmp_path, session)
    result = _resume_command("", tmp_path)

    assert result.action == "resume"
    assert "latest checkpoint" in result.ui_message
    assert "[Resume]" in result.ui_message
    assert "session:" in result.ui_message


def test_render_memory_command_shows_sessions_and_traces(tmp_path) -> None:
    from mokioclaw.interaction.commands import _memory_command
    from mokioclaw.reliability.session_store import create_session
    from mokioclaw.reliability.trace import TraceRecorder

    create_session(tmp_path, task="learn memory")
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="trace-demo")
    trace.end(status="finished", latest_node="final", final_state={"passed": True, "attempts": 1})

    result = _memory_command(tmp_path)

    assert result.action == "none"
    assert "Recent Sessions" in result.ui_message
    assert "Recent Traces" in result.ui_message
    assert "memory root" in result.ui_message


def test_resume_card_uses_sectioned_layout(tmp_path) -> None:
    from mokioclaw.interaction.commands import _resume_command
    from mokioclaw.reliability.session_store import create_session

    create_session(tmp_path, task="resume layout")
    result = _resume_command("", tmp_path)

    assert "Task:" in result.ui_message
    assert "Continue:" in result.ui_message


def test_status_card_uses_sectioned_layout(tmp_path) -> None:
    from mokioclaw.interaction.commands import _status_command

    result = _status_command(tmp_path)

    assert "Latest Session:" in result.ui_message
    assert "Commands:" in result.ui_message


def test_render_final_includes_summary_fields(capsys) -> None:
    render_final(
        {
            "passed": True,
            "attempts": 2,
            "final_status": "passed",
            "verifier_summary": "looks good",
            "repair_instruction": "none",
            "final_answer": "done",
        }
    )

    output = capsys.readouterr().out
    assert "status:" in output
    assert "attempts:" in output
    assert "verifier:" in output


def test_continue_command_aliases_resume(tmp_path) -> None:
    from mokioclaw.interaction.commands import dispatch_slash_command
    from mokioclaw.reliability.session_store import create_session

    create_session(tmp_path, task="alias")
    result = dispatch_slash_command("/continue", workspace=tmp_path)

    assert result.action == "resume"
    assert result.kind.name == "SYSTEM"


def test_permissions_command_lists_rules(tmp_path) -> None:
    from mokioclaw.interaction.commands import _permissions_command
    from mokioclaw.config.loader import load_user_config

    cfg = tmp_path / ".mokioclaw" / "config.md"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("---\nallowed_tools: [FileReadTool, GlobTool]\ndisallowed_tools: [BashTool]\n---\n", encoding="utf-8")

    result = _permissions_command("", tmp_path)

    assert result.action == "none"
    assert "Allowed" in result.ui_message
    assert "Disallowed" in result.ui_message
    assert "BashTool" in result.ui_message


def test_status_command_shows_model_and_trace(tmp_path) -> None:
    from mokioclaw.interaction.commands import _status_command
    from mokioclaw.reliability.session_store import create_session
    from mokioclaw.reliability.trace import TraceRecorder
    from mokioclaw.state.runtime import RuntimeState

    create_session(tmp_path, task="status task")
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="trace-demo")
    trace.end(status="finished", latest_node="final", final_state={"passed": True, "attempts": 1})

    result = _status_command(tmp_path)

    assert "agent mode:" in result.ui_message
    assert "trace mode:" in result.ui_message
    assert "Latest Trace" in result.ui_message


def test_permissions_add_and_remove(tmp_path) -> None:
    from mokioclaw.interaction.commands import _permissions_command

    result = _permissions_command("add deny BashTool", tmp_path)
    assert "BashTool" in result.ui_message

    result = _permissions_command("add allow mcp__*", tmp_path)
    assert "mcp__*" in result.ui_message

    result = _permissions_command("remove deny BashTool", tmp_path)
    assert "BashTool" not in result.ui_message


def test_permissions_reset(tmp_path) -> None:
    from mokioclaw.interaction.commands import _permissions_command

    _permissions_command("add deny BashTool", tmp_path)
    _permissions_command("add allow FileReadTool", tmp_path)

    result = _permissions_command("reset", tmp_path)
    assert "(none)" in result.ui_message


def test_permissions_persisted_and_loaded(tmp_path) -> None:
    from mokioclaw.interaction.commands import _permissions_command
    from mokioclaw.config.loader import load_user_config

    _permissions_command("add allow GlobTool", tmp_path)
    config = load_user_config(workspace=tmp_path)
    assert "GlobTool" in config.allowed_tools


def test_status_shows_model_and_account(tmp_path) -> None:
    from mokioclaw.interaction.commands import _status_command

    result = _status_command(tmp_path)

    assert "Model:" in result.ui_message
    assert "Account:" in result.ui_message
    assert "Permissions:" in result.ui_message
    assert "checkpoint mode:" in result.ui_message


def test_export_command_creates_file(tmp_path) -> None:
    from mokioclaw.interaction.commands import _export_command
    from mokioclaw.reliability.session_store import create_session

    create_session(tmp_path, task="export test")
    result = _export_command("", tmp_path)

    assert result.action == "none"
    assert "path:" in result.ui_message
    import json
    meta = result.meta
    export_path = Path(meta["export_path"])
    assert export_path.exists()
    content = export_path.read_text(encoding="utf-8")
    assert "export test" in content


def test_export_command_json_format(tmp_path) -> None:
    from mokioclaw.interaction.commands import _export_command
    from mokioclaw.reliability.session_store import create_session

    create_session(tmp_path, task="json export")
    result = _export_command("json", tmp_path)

    export_path = Path(result.meta["export_path"])
    assert export_path.suffix == ".json"
    import json
    data = json.loads(export_path.read_text(encoding="utf-8"))
    assert data["task"] == "json export"


def test_branch_forks_latest_session(tmp_path) -> None:
    from mokioclaw.interaction.commands import _branch_command
    from mokioclaw.reliability.session_store import create_session, append_user_turn, save_session, load_session

    session = create_session(tmp_path, task="original task")
    save_session(tmp_path, session)

    result = _branch_command("", tmp_path)

    assert result.action == "branch"
    assert "new session:" in result.ui_message
    new_sid = result.meta["session_id"]
    assert new_sid != session["session_id"]
    forked = load_session(tmp_path, new_sid)
    assert forked is not None
    assert forked.get("forked_from") == session["session_id"]
    assert forked.get("task") == "original task"
    assert len(forked.get("turns", [])) == len(session.get("turns", []))


def test_branch_with_new_task(tmp_path) -> None:
    from mokioclaw.interaction.commands import _branch_command
    from mokioclaw.reliability.session_store import create_session, save_session, load_session

    session = create_session(tmp_path, task="old task")
    save_session(tmp_path, session)

    result = _branch_command(f"{session['session_id']} new task direction", tmp_path)

    assert result.action == "branch"
    forked = load_session(tmp_path, result.meta["session_id"])
    assert forked["task"] == "new task direction"


def test_branch_not_found(tmp_path) -> None:
    from mokioclaw.interaction.commands import _branch_command

    result = _branch_command("session-nonexistent", tmp_path)

    assert result.action == "none"
    assert "not found" in result.ui_message.lower()


def test_cd_command_switches_dir(tmp_path) -> None:
    from mokioclaw.interaction.commands import _cd_command

    target = tmp_path / "subdir"
    target.mkdir()

    result = _cd_command("subdir", tmp_path)

    assert result.action == "cd"
    assert "cwd:" in result.ui_message
    assert str(target) in result.meta["cwd"]


def test_cd_command_rejects_outside_workspace(tmp_path) -> None:
    from mokioclaw.interaction.commands import _cd_command

    result = _cd_command("../outside", tmp_path)

    assert result.action == "none"
    assert "outside workspace" in result.ui_message.lower()


def test_cd_command_resets_to_root(tmp_path) -> None:
    from mokioclaw.interaction.commands import _cd_command

    result = _cd_command("", tmp_path)

    assert result.action == "cd"
    assert "reset" in result.ui_message.lower()


def test_loop_command_parses_interval(tmp_path) -> None:
    from mokioclaw.interaction.commands import _loop_command

    result = _loop_command("30 check status", tmp_path)

    assert result.action == "loop_start"
    assert result.meta["interval"] == 30
    assert "check status" in result.meta["prompt"]


def test_loop_command_stop(tmp_path) -> None:
    from mokioclaw.interaction.commands import _loop_command

    result = _loop_command("stop", tmp_path)

    assert result.action == "loop_stop"


def test_loop_command_invalid_interval(tmp_path) -> None:
    from mokioclaw.interaction.commands import _loop_command

    result = _loop_command("abc something", tmp_path)

    assert result.action == "none"


def test_fuzzy_matching_subsequence(tmp_path) -> None:
    from mokioclaw.interaction.commands import filter_command_suggestions

    suggestions = filter_command_suggestions("pr", tmp_path)
    assert "permissions" in suggestions


def test_batch_parses_parallel(tmp_path) -> None:
    from mokioclaw.interaction.commands import _batch_command

    result = _batch_command("task1 | task2 | task3", tmp_path)

    assert result.action == "batch"
    assert result.meta["sequential"] is False
    assert len(result.meta["tasks"]) == 3


def test_batch_parses_sequential(tmp_path) -> None:
    from mokioclaw.interaction.commands import _batch_command

    result = _batch_command("--seq task1 | task2", tmp_path)

    assert result.action == "batch"
    assert result.meta["sequential"] is True
    assert len(result.meta["tasks"]) == 2


def test_batch_empty_returns_usage(tmp_path) -> None:
    from mokioclaw.interaction.commands import _batch_command

    result = _batch_command("", tmp_path)

    assert result.action == "none"
    assert "Usage" in result.ui_message


def test_model_command_shows_current(tmp_path) -> None:
    from mokioclaw.interaction.commands import _model_command

    result = _model_command("", tmp_path)

    assert result.action == "none"
    assert "current:" in result.ui_message


def test_model_command_switches(tmp_path) -> None:
    from mokioclaw.interaction.commands import _model_command

    result = _model_command("gpt-4o", tmp_path)

    assert result.action == "model_switch"
    assert "gpt-4o" in result.ui_message
    assert (tmp_path / ".mokioclaw" / "model_override").exists()


def test_cost_command_shows_tokens(tmp_path) -> None:
    from mokioclaw.interaction.commands import _cost_command
    from mokioclaw.reliability.trace import TraceRecorder
    from mokioclaw.state.runtime import RuntimeState

    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="cost test")
    trace.record_token_usage(100, 50)
    trace.end(status="finished", latest_node="final", final_state={"passed": True, "attempts": 1})

    result = _cost_command(tmp_path)

    assert result.action == "none"
    assert "prompt tokens:" in result.ui_message
    assert "150" in result.ui_message


def test_init_command_creates_config(tmp_path) -> None:
    from mokioclaw.interaction.commands import _init_command

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    result = _init_command("", tmp_path)

    assert result.action == "none"
    assert (tmp_path / ".mokioclaw" / "config.md").exists()
    assert (tmp_path / ".claude" / "rules").exists()


def test_review_command_shows_diff(tmp_path) -> None:
    from mokioclaw.interaction.commands import _review_command

    result = _review_command("", tmp_path)

    assert result.action == "review"
    assert "Diff Stat:" in result.ui_message


def test_rules_globs_frontmatter(tmp_path) -> None:
    from mokioclaw.config.loader import format_glob_rules_for_read, load_user_config, matching_glob_rules

    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "python-style.md").write_text(
        '---\nglobs: ["**/*.py"]\n---\nUse 4-space indentation.\n', encoding="utf-8"
    )
    (rules_dir / "always.md").write_text(
        "Always answer concisely.\n", encoding="utf-8"
    )

    config = load_user_config(workspace=tmp_path)

    # globs 规则不再无条件进 custom_instructions，而是按文件命中注入
    assert "Always answer concisely." in config.custom_instructions
    assert "4-space indentation" not in config.custom_instructions
    assert len(config.glob_rules) == 1
    assert config.glob_rules[0]["name"] == "python-style.md"
    assert config.glob_rules[0]["globs"] == ["**/*.py"]

    # 命中 .py 文件（含嵌套路径）
    matched = matching_glob_rules(tmp_path, tmp_path / "src" / "pkg" / "mod.py")
    assert len(matched) == 1
    assert "4-space indentation" in format_glob_rules_for_read(matched)
    # 不命中非 .py 文件
    assert matching_glob_rules(tmp_path, tmp_path / "docs" / "readme.md") == []
