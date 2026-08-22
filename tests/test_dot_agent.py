"""dot 包独立测试 — 自定义介入 / 用户代码回滚 / agent_mode / 渐进披露分发

不依赖真实 LLM：monkeypatch dot.graph.coding_graph.create_model 为抛异常的
fake，让 plan 生成失败 → 空 plan → replan 达上限 → human_intervene 置
awaiting_intervention → finally 持久化本轮 → 控制台 resume。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import dot.graph.coding_graph as cg
from dot.host.agent_host import AgentHost
from dot.core.approval import ApprovalDecision, ApprovalRequest
from dot.core.runtime import RuntimeState
from dot.tools.meta import build_tools_for_session, dispatch_special_tool


# ============================================================
# Fixtures / helpers
# ============================================================

@pytest.fixture()
def no_llm(monkeypatch):
    """把 coding_graph 的 create_model 替换为抛异常的 fake（不触网）"""
    def fake_model():
        raise RuntimeError("no llm in test")

    monkeypatch.setattr(cg, "create_model", fake_model)
    return fake_model


@pytest.fixture()
def env(tmp_path: Path, no_llm) -> AgentHost:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    host = AgentHost(workspace=workspace, sessions_root=tmp_path / "sessions")
    return host


def _run(host: AgentHost, text: str, mode: str = "auto") -> str:
    """跑一轮，返回 final_answer"""
    final = ""
    for chunk in host.run(text, agent_mode=mode):
        for node, update in chunk.items():
            if node == "finally_node" and isinstance(update, dict):
                final = update.get("final_answer", "")
    return final


# ============================================================
# 图结构
# ============================================================

class TestGraphStructure:
    def test_build_graph_nodes(self):
        compiled = cg.compile_graph()
        node_names = set(compiled.get_graph().nodes.keys())
        for expected in (
            "context_compress", "plan_node", "coding_agent",
            "valid_node", "human_intervene", "finally_node",
        ):
            assert expected in node_names, f"missing node: {expected}"

    def test_state_schema_single_channel(self):
        hints = cg.DotAgentState.__annotations__
        assert "session" in hints


# ============================================================
# 自定义介入（无 langgraph interrupt/checkpoint）
# ============================================================

class TestCustomIntervention:
    def test_intervention_flow(self, env):
        """无 LLM → replan 耗尽 → 介入 → finally 持久化 turn 1"""
        final = _run(env, "do something")
        assert final, "finally_node should emit final_answer"

        session = env.get_or_create_session()
        assert session.awaiting_intervention is True
        assert session.replan_count >= session.replan_max
        assert session.current_turn_id == 1

        # 持久化校验（finally 节点内完成）
        persistence = session.persistence
        assert persistence is not None
        assert 1 in persistence.list_available_turns(session.session_id)
        meta = persistence.load_session_meta(session.session_id)
        assert meta["current_turn_id"] == 1
        assert meta["awaiting_intervention"] is True
        assert meta["task"] == "do something"
        assert len(meta["messages"]) >= 1

    def test_resume_stop(self, env):
        """stop：清标记，不加 turn"""
        _run(env, "task a")
        session = env.get_or_create_session()
        sid = session.session_id
        before = session.current_turn_id

        for _ in env.resume_intervention("stop"):
            pass

        assert session.awaiting_intervention is False
        assert session.current_turn_id == before
        assert env.has_pending_intervention(sid) is False

    def test_resume_continue_reenters_graph(self, env):
        """continue：计数清零重新进图（无 LLM 会再次介入，turn+1）"""
        _run(env, "task b")
        session = env.get_or_create_session()
        assert session.current_turn_id == 1

        final = ""
        for chunk in env.resume_intervention("continue"):
            for node, update in chunk.items():
                if node == "finally_node" and isinstance(update, dict):
                    final = update.get("final_answer", "")

        assert session.current_turn_id == 2
        assert session.awaiting_intervention is True  # 无 LLM 再次介入
        assert final

    def test_intervention_survives_process_restart(self, env):
        """介入状态跨进程恢复（靠 session.json，不用 checkpoint）"""
        _run(env, "paused task")
        sid = env.get_or_create_session().session_id

        host2 = AgentHost(workspace=env.workspace, sessions_root=env.session_manager.sessions_root)
        assert host2.has_pending_intervention(sid) is True

        for _ in host2.resume_intervention("stop"):
            pass
        assert host2.has_pending_intervention(sid) is False


# ============================================================
# 用户代码回滚（agent 专用 git）
# ============================================================

class TestCodeRewind:
    def test_rewind_restores_user_code(self, env):
        """turn1 提交 v1 → 改成 v2 → turn2 提交 → rewind(1) 恢复 v1"""
        ws = env.workspace
        target = ws / "src" / "app.py"
        target.parent.mkdir(exist_ok=True)
        target.write_text("v1", encoding="utf-8")

        _run(env, "turn one")  # turn 1 commit（代码 v1）

        target.write_text("v2-modified-by-agent", encoding="utf-8")
        _run(env, "turn two")  # turn 2 commit（代码 v2）

        session = env.get_or_create_session()
        assert session.current_turn_id == 2

        env.rewind_to_turn(1, session.session_id)

        assert target.read_text(encoding="utf-8") == "v1"
        assert session.current_turn_id == 1

    def test_agent_git_is_isolated(self, env):
        """agent repo 在 .dot/git，不污染用户目录（无 .git 出现在工作区根）"""
        _run(env, "isolation check")
        assert (env.workspace / ".dot" / "git" / "HEAD").exists()
        assert not (env.workspace / ".git").exists()

    def test_rewind_restores_memory_state(self, env):
        """rewind 同时回滚内存 messages（修复旧版只写盘 bug）"""
        _run(env, "first task")
        _run(env, "second task")
        session = env.get_or_create_session()
        sid = session.session_id
        msgs_after_turn2 = len(session.messages)
        assert session.current_turn_id == 2

        env.rewind_to_turn(1, sid)
        assert session.current_turn_id == 1
        assert len(session.messages) < msgs_after_turn2


# ============================================================
# per-turn 状态
# ============================================================

class TestTurnState:
    def test_task_survives_reset(self, env):
        """回归：reset_per_turn 在 append 之后执行会把 task 清空"""
        _run(env, "my important task")
        assert env.get_or_create_session().task == "my important task"

    def test_no_empty_session_id_dir(self, env):
        """回归：旧版 session_id='' 会创建空目录 session"""
        session = env.get_or_create_session()
        assert session.session_id != ""
        session_dir = env.session_manager.sessions_root / session.session_id
        assert (session_dir / "session.json").is_file()


# ============================================================
# 会话恢复
# ============================================================

class TestSessionManager:
    def test_get_or_create_reuses_memory(self, env):
        s1 = env.get_or_create_session()
        s2 = env.get_or_create_session()
        assert s1 is s2

    def test_latest_session_restored(self, env):
        _run(env, "persisted task")
        for _ in env.resume_intervention("stop"):
            pass
        sid = env.get_or_create_session().session_id

        host2 = AgentHost(workspace=env.workspace, sessions_root=env.session_manager.sessions_root)
        session = host2.get_or_create_session()
        assert session.session_id == sid
        assert session.current_turn_id == 1
        assert 1 in session.persistence.list_available_turns(sid)


# ============================================================
# agent 工作模式（fix.md：plan / edit / auto）
# ============================================================

class TestAgentModes:
    def test_plan_mode_readonly_tools(self, env):
        session = env.get_or_create_session()
        tools = {t.name for t in build_tools_for_session(session)}
        env.session_manager.apply_runtime_config(session, agent_mode="plan")
        plan_tools = {t.name for t in build_tools_for_session(session)}
        assert "FileWriteTool" not in plan_tools
        assert "FileEditTool" not in plan_tools
        assert "FileReadTool" in plan_tools
        assert plan_tools < tools  # plan 是全量工具的子集
        # 元工具仍可用（只读）
        assert "skill_search" in plan_tools

    def test_auto_mode_full_tools(self, env):
        session = env.get_or_create_session()
        env.session_manager.apply_runtime_config(session, agent_mode="auto")
        names = {t.name for t in build_tools_for_session(session)}
        assert {"FileWriteTool", "FileEditTool", "FileReadTool", "BashTool"} <= names

    def test_edit_mode_bash_requires_approval_every_time(self, tmp_path):
        """edit 模式：普通命令也走审批；auto 模式直接放行"""
        from dot.tools import build_tools

        state = RuntimeState(workspace=tmp_path, agent_mode="edit", approval_mode="inline")
        decisions = []

        def handler(request: ApprovalRequest) -> ApprovalDecision:
            decisions.append(request)
            return ApprovalDecision(approved=True)

        state.approval_handler = handler
        tools = {t.name: t for t in build_tools(state)}

        # echo 是无害命令，edit 模式下也要审批
        r1 = tools["BashTool"].invoke({"command": "echo edit-mode"})
        assert r1["ok"] is True
        assert r1.get("requires_approval") is True
        assert len(decisions) == 1

        # auto 模式：同命令不触发审批
        state.agent_mode = "auto"
        state.approval_mode = "auto"
        r2 = tools["BashTool"].invoke({"command": "echo auto-mode"})
        assert r2["ok"] is True
        assert r2.get("requires_approval") is None
        assert len(decisions) == 1

    def test_edit_mode_bash_rejected(self, tmp_path):
        """edit 模式：审批拒绝 → 命令不执行"""
        from dot.tools import build_tools

        state = RuntimeState(workspace=tmp_path, agent_mode="edit", approval_mode="inline")
        state.approval_handler = lambda req: ApprovalDecision(approved=False, reason="no")
        tools = {t.name: t for t in build_tools(state)}
        result = tools["BashTool"].invoke({"command": "echo denied"})
        assert result["ok"] is False
        assert result.get("approved") is False


# ============================================================
# 渐进披露命名与分发
# ============================================================

class TestProgressiveDisclosure:
    def test_skill_prefix_dispatch(self, env):
        """skill_ 前缀调用 → 返回 skill 完整内容"""
        ws = env.workspace
        sd = ws / ".dot" / "skills" / "demo"
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(
            "---\nname: demo\ndescription: a demo skill\n---\nDo the demo thing.",
            encoding="utf-8",
        )
        # 重建 session 让 host 重新扫描
        env.session_manager.destroy_session()
        session = env.get_or_create_session()

        assert "skill_demo" in session.skill_host.get_all_skill_names()
        assert "skill_demo" in session.skill_host.get_catalog_text()

        msg = dispatch_special_tool(session, {"name": "skill_demo", "args": {}, "id": "c1"})
        assert msg is not None
        assert "Do the demo thing" in msg.content

    def test_mcp_dispatch_unknown_tool(self, env):
        """mcp_ 前缀未知工具 → 返回错误 + 可用目录提示"""
        session = env.get_or_create_session()
        msg = dispatch_special_tool(session, {"name": "mcp_nope_missing", "args": {}, "id": "c2"})
        assert msg is not None
        assert "unknown mcp tool" in msg.content

    def test_system_tool_not_intercepted(self, env):
        """系统工具（无前缀）不被特殊分发拦截"""
        session = env.get_or_create_session()
        msg = dispatch_special_tool(session, {"name": "FileReadTool", "args": {}, "id": "c3"})
        assert msg is None


# ============================================================
# 工具层防护（回归）
# ============================================================

class TestToolGuards:
    def test_read_before_edit(self, tmp_path):
        from dot.tools import build_tools

        state = RuntimeState(workspace=tmp_path)
        tools = {t.name: t for t in build_tools(state)}
        target = tmp_path / "src" / "a.txt"
        target.parent.mkdir()
        target.write_text("line1\nline2", encoding="utf-8")

        result = tools["FileEditTool"].invoke(
            {"file_path": "src/a.txt", "old_text": "line1", "new_text": "x"}
        )
        assert result["ok"] is False
        assert "not been read" in result["error"]

        tools["FileReadTool"].invoke({"file_path": "src/a.txt"})
        result = tools["FileEditTool"].invoke(
            {"file_path": "src/a.txt", "old_text": "line1", "new_text": "edited"}
        )
        assert result["ok"] is True

    def test_dangerous_command_blocked(self, tmp_path):
        from dot.tools import build_tools

        state = RuntimeState(workspace=tmp_path, approval_mode="auto")
        tools = {t.name: t for t in build_tools(state)}
        result = tools["BashTool"].invoke({"command": "rm -rf /"})
        assert result["ok"] is False

    def test_workspace_escape_blocked(self, tmp_path):
        from dot.tools import build_tools

        state = RuntimeState(workspace=tmp_path)
        tools = {t.name: t for t in build_tools(state)}
        result = tools["FileWriteTool"].invoke(
            {"file_path": "../outside.txt", "content": "nope"}
        )
        assert result["ok"] is False
