"""Harness 对齐 Claude Code 的关键行为回归测试。"""
from __future__ import annotations

import json
from pathlib import Path

from mokioclaw.core.hooks import Hook, HookEvent, HookPayload, HookResult, HookRunner
from mokioclaw.core.tool_result_budget import ToolResultBudget
from mokioclaw.interaction.commands import CommandKind, dispatch_slash_command
from mokioclaw.prompts.builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY, PromptBuilder
from mokioclaw.prompts.thinking import apply_thinking_mode, parse_thinking_mode
from mokioclaw.reliability.parallel import are_tools_independent
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools.file_tools import edit_file, read_file, write_file
from mokioclaw.tools.skill import discover_skills


def test_thinking_mode_ultrathink():
    cleaned, instruction = apply_thinking_mode("ultrathink fix the bug in auth")
    assert cleaned == "fix the bug in auth"
    assert "ULTRATHINK" in instruction
    assert parse_thinking_mode("hello") is None


def test_prompt_builder_has_dynamic_boundary(tmp_path: Path):
    builder = PromptBuilder(workspace=tmp_path)
    prompt = builder.build("code_agent")
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in prompt
    assert "Current working directory" in prompt


def test_session_hooks_do_not_run_tool_hooks():
    runner = HookRunner()
    calls: list[str] = []

    def tool_hook(payload: HookPayload) -> HookResult:
        calls.append("tool")
        return HookResult()

    def session_hook(payload: HookPayload) -> HookResult:
        calls.append("session")
        return HookResult()

    runner.register(Hook(name="bash", matcher=r"^Bash", handler=tool_hook))
    runner.register(
        Hook(
            name="start",
            events=(HookEvent.SessionStart,),
            handler=session_hook,
        )
    )
    runner.run(HookEvent.SessionStart, HookPayload(event=HookEvent.SessionStart))
    assert calls == ["session"]


def test_partial_read_blocks_edit(tmp_path: Path):
    runtime = RuntimeState(workspace=tmp_path)
    path = tmp_path / "a.py"
    path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    read_file(runtime, "a.py", offset=0, limit=1)
    result = edit_file(runtime, "a.py", "line1", "LINE1")
    assert result["ok"] is False
    assert "partially read" in result["error"]


def test_full_read_allows_edit(tmp_path: Path):
    runtime = RuntimeState(workspace=tmp_path)
    path = tmp_path / "a.py"
    path.write_text("line1\nline2\n", encoding="utf-8")
    read_file(runtime, "a.py")
    result = edit_file(runtime, "a.py", "line1", "LINE1")
    assert result["ok"] is True


def test_mutating_tools_are_serial():
    calls = [
        {"name": "FileReadTool", "args": {"file_path": "a.py"}},
        {"name": "BashTool", "args": {"command": "echo hi"}},
    ]
    assert are_tools_independent(calls) is False
    reads = [
        {"name": "FileReadTool", "args": {"file_path": "a.py"}},
        {"name": "GrepTool", "args": {"pattern": "x"}},
    ]
    assert are_tools_independent(reads) is True


def test_tool_result_budget_spills(tmp_path: Path):
    budget = ToolResultBudget(max_chars=100)
    result = budget.apply({"ok": True, "stdout": "x" * 5000}, "BashTool", tmp_path)
    assert result.get("_truncated") is True
    assert Path(result["_full_output_path"]).exists()


def test_slash_help_and_custom_command(tmp_path: Path):
    cmd_dir = tmp_path / ".mokioclaw" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "hello.md").write_text("Say hello politely.", encoding="utf-8")
    help_result = dispatch_slash_command("/help", workspace=tmp_path)
    assert help_result.kind == CommandKind.SYSTEM
    assert "[Help]" in help_result.ui_message
    assert "/resume" in help_result.ui_message
    status_result = dispatch_slash_command("/status", workspace=tmp_path)
    assert status_result.kind == CommandKind.SYSTEM
    assert "[Status]" in status_result.ui_message
    memory_result = dispatch_slash_command("/memory", workspace=tmp_path)
    assert memory_result.kind == CommandKind.SYSTEM
    assert "[Memory]" in memory_result.ui_message
    custom = dispatch_slash_command("/hello", workspace=tmp_path)
    assert custom.kind == CommandKind.CUSTOM
    assert "Say hello" in custom.inject_message


def test_skill_discovery_from_skill_md(tmp_path: Path):
    skill_dir = tmp_path / ".mokioclaw" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\ninvoke: manual\n---\n\n# Demo\nDo the demo.\n",
        encoding="utf-8",
    )
    skills = discover_skills(tmp_path / ".mokioclaw" / "skills")
    assert len(skills) == 1
    assert skills[0].name == "demo"


def test_load_mcp_tool_ordered_first():
    from mokioclaw.agents.code_agent import _order_tool_calls_for_execution

    calls = [
        {"name": "Foo", "id": "1"},
        {"name": "LoadMcpTool", "id": "2"},
        {"name": "Bar", "id": "3"},
    ]
    ordered = _order_tool_calls_for_execution(calls)
    assert [c["name"] for c in ordered] == ["LoadMcpTool", "Foo", "Bar"]


def test_end_persistent_session_hooks(tmp_path: Path):
    from mokioclaw.orchestration.agent import end_persistent_session_hooks
    from mokioclaw.reliability.session_store import create_session, load_session, save_session

    session = create_session(tmp_path, "test task")
    session["_session_hooks_started"] = True
    save_session(tmp_path, session)
    end_persistent_session_hooks(tmp_path)
    saved = load_session(tmp_path, session["session_id"])
    assert saved.get("_session_hooks_started") is False
    assert saved.get("_session_ended") is True
    end_persistent_session_hooks(tmp_path)  # idempotent


def test_snip_compacts_old_tool_results():
    from langchain_core.messages import AIMessage, ToolMessage
    from mokioclaw.memory.snip import snip_compact_if_needed

    msgs = [AIMessage(content="start", id="a0")]
    for i in range(20):
        msgs.append(
            ToolMessage(
                content="x" * 500,
                name="FileReadTool",
                tool_call_id=f"t{i}",
                id=f"msg-{i:05d}",
            )
        )
    snipped, freed = snip_compact_if_needed(msgs, keep_recent_tools=4, min_messages=10)
    assert freed > 0
    assert any("[snip]" in str(getattr(m, "content", "")) for m in snipped)


def test_hook_permission_decision_deny():
    from mokioclaw.core.hooks import Hook, HookEvent, HookPayload, HookResult, HookRunner

    runner = HookRunner()

    def deny_env(payload: HookPayload) -> HookResult:
        path = str((payload.tool_args or {}).get("file_path", ""))
        if path.endswith(".env"):
            return HookResult(
                permission_decision="deny",
                feedback="Cannot modify .env files",
            )
        return HookResult()

    runner.register(
        Hook(name="protect-env", events=(HookEvent.PreToolUse,), handler=deny_env)
    )
    result = runner.run(
        HookEvent.PreToolUse,
        HookPayload(
            event=HookEvent.PreToolUse,
            tool_name="FileWriteTool",
            tool_args={"file_path": ".env"},
        ),
    )
    assert result.blocked is True
    assert "env" in result.feedback.lower()


def test_tool_search_defers_and_loads(tmp_path: Path):
    from mokioclaw.state.runtime import RuntimeState
    from mokioclaw.tools.registry import build_tools
    from mokioclaw.tools.tool_search import tool_search_enabled

    if not tool_search_enabled():
        return
    rt = RuntimeState(workspace=tmp_path)
    tools = build_tools(rt)
    names = {t.name for t in tools}
    assert "ToolSearchTool" in names
    assert "WebSearchTool" not in names  # deferred until search
    search = next(t for t in tools if t.name == "ToolSearchTool")
    result = search.invoke({"query": "select:WebSearchTool"})
    assert result.get("ok") is True
    tools2 = build_tools(rt)
    assert "WebSearchTool" in {t.name for t in tools2}


def test_context_modifier_cd(tmp_path: Path):
    from mokioclaw.state.runtime import RuntimeState
    from mokioclaw.tools.bash_tool import run_bash

    sub = tmp_path / "src"
    sub.mkdir()
    rt = RuntimeState(workspace=tmp_path)
    result = run_bash(rt, "cd src")
    assert result["ok"] is True
    assert "_context_modifier" in result
    from mokioclaw.core.context_modifier import apply_context_modifier

    apply_context_modifier(rt, result)
    assert rt.cwd == sub.resolve()


def test_classifier_and_sandbox_and_depth(tmp_path: Path):
    from mokioclaw.security.classifier import ClassifierDecision, fast_path_decision
    from mokioclaw.security.sandbox import check_sandbox
    from mokioclaw.state.runtime import RuntimeState
    from mokioclaw.tools.agent_tool import _filter_child_tools, _spawn_child_runtime

    assert fast_path_decision("BashTool", {"command": "git status"}) is ClassifierDecision.ALLOW
    assert fast_path_decision("BashTool", {"command": "rm -rf /"}) is ClassifierDecision.DENY

    rt = RuntimeState(workspace=tmp_path)
    assert check_sandbox(rt, "ls") is None
    assert check_sandbox(rt, "cat /etc/passwd") is not None

    child = _spawn_child_runtime(rt)
    deep = child
    for _ in range(3):
        deep = _spawn_child_runtime(deep)
    assert deep._subagent_depth >= 3
    names = [t.name for t in _filter_child_tools(deep, None, disable_nested_agent=False)]
    assert "AgentTool" not in names
