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
# agent 工作模式（fix-权限控制.md：PermissionManager 三级拦截 + 三态决策）
# ============================================================

@pytest.fixture()
def pm(tmp_path):
    """干净的全局 PermissionManager（指向 tmp workspace）"""
    from dot.core.permission import get_permission_manager, reset_permission_manager

    reset_permission_manager()
    manager = get_permission_manager()
    manager.load_project(tmp_path)
    yield manager
    reset_permission_manager()


class TestAgentModes:
    def test_plan_mode_registers_write_tools(self, env):
        """fix-权限控制.md：plan 也注册写工具，写操作由权限层 ASK 拦截"""
        session = env.get_or_create_session()
        env.session_manager.apply_runtime_config(session, agent_mode="plan")
        names = {t.name for t in build_tools_for_session(session)}
        assert {"FileWriteTool", "FileEditTool", "FileReadTool", "BashTool"} <= names
        assert "skill_search" in names

    def test_auto_mode_full_tools(self, env):
        session = env.get_or_create_session()
        env.session_manager.apply_runtime_config(session, agent_mode="auto")
        names = {t.name for t in build_tools_for_session(session)}
        assert {"FileWriteTool", "FileEditTool", "FileReadTool", "BashTool"} <= names


class TestPermissionManager:
    """三级拦截 + 三态决策（doc/fix-权限控制.md）"""

    def _check(self, pm, tool, args, mode="auto", **kw):
        from dot.core.permission import Decision

        d = pm.check(tool, args, agent_mode=mode, workspace=pm._workspace, **kw)
        return d

    # ---- 模式规则（第 3 级）----

    def test_plan_mode_rules(self, pm):
        assert self._check(pm, "BashTool", {"command": "echo hi"}, "plan").decision.value == "deny"
        assert self._check(pm, "FileWriteTool", {"file_path": "src/a.py", "content": "x"}, "plan").decision.value == "ask"
        assert self._check(pm, "FileEditTool", {"file_path": "src/a.py", "old_text": "a", "new_text": "b"}, "plan").decision.value == "ask"
        assert self._check(pm, "FileReadTool", {"file_path": "src/a.py"}, "plan").decision.value == "allow"
        assert self._check(pm, "GrepTool", {"pattern": "x"}, "plan").decision.value == "allow"

    def test_edit_mode_rules(self, pm):
        assert self._check(pm, "BashTool", {"command": "echo hi"}, "edit").decision.value == "ask"
        assert self._check(pm, "FileWriteTool", {"file_path": "src/a.py", "content": "x"}, "edit").decision.value == "allow"
        assert self._check(pm, "FileReadTool", {"file_path": "src/a.py"}, "edit").decision.value == "allow"

    def test_auto_mode_rules(self, pm):
        """auto 全放行（高风险命令 pip/curl 也放行——用户决策）"""
        assert self._check(pm, "BashTool", {"command": "pip install requests"}, "auto").decision.value == "allow"
        assert self._check(pm, "BashTool", {"command": "curl http://x.com"}, "auto").decision.value == "allow"
        assert self._check(pm, "FileWriteTool", {"file_path": "src/a.py", "content": "x"}, "auto").decision.value == "allow"

    def test_mcp_skill_not_gated(self, pm):
        """mcp_/skill_ 本期不做权限校验（用户决策）"""
        assert self._check(pm, "mcp_amap_maps_geocode", {"address": "x"}, "plan").decision.value == "allow"
        assert self._check(pm, "skill_demo", {}, "plan").decision.value == "allow"
        assert self._check(pm, "mcp_search", {"tool_name": "x"}, "plan").decision.value == "allow"

    # ---- 系统黑名单（第 1 级，人工确认也不可放行）----

    def test_system_blacklist_bash(self, pm):
        for mode in ("plan", "edit", "auto"):
            d = self._check(pm, "BashTool", {"command": "rm -rf /"}, mode)
            assert d.decision.value == "deny" and d.source == "system"
        # approved=True（人工已确认）也拦不住系统黑名单
        d = self._check(pm, "BashTool", {"command": "rm -rf /"}, "edit", approved=True)
        assert d.decision.value == "deny"

    def test_system_blacklist_sensitive_file(self, pm):
        d = self._check(pm, "FileReadTool", {"file_path": "secrets.pem"}, "auto")
        assert d.decision.value == "deny" and d.source == "system"
        d = self._check(pm, "FileWriteTool", {"file_path": "src/a.py", "content": "x"}, "auto")
        assert d.decision.value == "allow"

    def test_write_whitelist_kept(self, pm):
        """写目录白名单保留：非白名单目录写 = 系统 DENY"""
        d = self._check(pm, "FileWriteTool", {"file_path": "random_dir/a.py", "content": "x"}, "auto")
        assert d.decision.value == "deny" and d.source == "system"

    # ---- 项目黑名单（第 2 级）----

    def test_project_config_file_patterns(self, pm, tmp_path):
        import json as _json

        (tmp_path / ".agent-security.json").write_text(
            _json.dumps({
                "denyFilePatterns": ["**/.env", "**/config/secret/**"],
                "denyBashRegex": [r"curl\s+http"],
            }),
            encoding="utf-8",
        )
        pm.load_project(tmp_path)

        # 读也拦（文档：作用于所有文件工具）
        d = self._check(pm, "FileReadTool", {"file_path": "config/secret/db.yaml"}, "auto")
        assert d.decision.value == "deny" and d.source == "project"
        assert "agent-security.json" in d.deny_message()
        # 写拦
        d = self._check(pm, "FileWriteTool", {"file_path": ".env", "content": "x"}, "auto")
        assert d.decision.value == "deny" and d.source == "project"
        # 未命中放行
        assert self._check(pm, "FileReadTool", {"file_path": "src/a.py"}, "auto").decision.value == "allow"
        # bash 正则拦
        d = self._check(pm, "BashTool", {"command": "curl http://evil.com"}, "auto")
        assert d.decision.value == "deny" and d.source == "project"

    def test_project_config_invalid_json_discarded(self, pm, tmp_path):
        """非法 JSON：丢弃整套 + 不崩溃 + 回退系统默认"""
        (tmp_path / ".agent-security.json").write_text("{not valid json", encoding="utf-8")
        pm.load_project(tmp_path)
        assert self._check(pm, "FileReadTool", {"file_path": "anything.txt"}, "auto").decision.value == "allow"

    def test_project_config_unknown_keys_ignored(self, pm, tmp_path):
        """未知 Key 忽略不报错；allow 类字段无效（只增不减铁律）"""
        import json as _json

        (tmp_path / ".agent-security.json").write_text(
            _json.dumps({
                "allowEverything": True,
                "whiteList": ["x"],
                "denyFilePatterns": ["**/private/**"],
            }),
            encoding="utf-8",
        )
        pm.load_project(tmp_path)
        assert self._check(pm, "FileReadTool", {"file_path": "src/a.py"}, "auto").decision.value == "allow"
        assert self._check(pm, "FileReadTool", {"file_path": "private/k.txt"}, "auto").decision.value == "deny"

    def test_project_deny_not_overridable_by_approval(self, pm, tmp_path):
        import json as _json

        (tmp_path / ".agent-security.json").write_text(
            _json.dumps({"denyBashRegex": [r"git\s+push"]}), encoding="utf-8"
        )
        pm.load_project(tmp_path)
        d = self._check(pm, "BashTool", {"command": "git push origin"}, "edit", approved=True)
        assert d.decision.value == "deny" and d.source == "project"

    # ---- ASK 审批链路 ----

    def test_ask_flow_approve_then_recheck(self, pm):
        """ASK → 用户确认 → approved 重走 → 模式层放行（黑名单仍全量校验）"""
        pm.set_approval_handler(lambda info: True)
        assert pm.ask_user("BashTool", {"command": "echo hi"}, self._check(pm, "BashTool", {"command": "echo hi"}, "edit"), agent_mode="edit") is True
        d = self._check(pm, "BashTool", {"command": "echo hi"}, "edit", approved=True)
        assert d.decision.value == "allow" and "approved-once" in d.reason

    def test_ask_flow_user_reject(self, pm):
        pm.set_approval_handler(lambda info: False)
        decision = self._check(pm, "BashTool", {"command": "echo hi"}, "edit")
        assert decision.decision.value == "ask"
        assert pm.ask_user("BashTool", {"command": "echo hi"}, decision, agent_mode="edit") is False

    def test_headless_ask_degrades_to_deny(self, pm):
        """无头（无审批入口）：ASK 自动降级拒绝，不卡死（文档 §10.4.3）"""
        pm.set_approval_handler(None)
        decision = self._check(pm, "BashTool", {"command": "echo hi"}, "edit")
        assert decision.decision.value == "ask"
        assert pm.ask_user("BashTool", {"command": "echo hi"}, decision, agent_mode="edit") is False

    # ---- 集成：_run_tool_call 走权限 ----

    def test_run_tool_call_denied_message(self, pm):
        """DENY 返回拦截 ToolMessage，文案区分来源"""
        import dot.graph.coding_graph as cg
        from dot.session import Session

        session = Session(session_id="perm-test", workspace=pm._workspace)
        runtime = RuntimeState(workspace=pm._workspace, agent_mode="plan")
        msg = cg._run_tool_call(session, runtime, [], {"name": "BashTool", "args": {"command": "echo hi"}, "id": "c1"}, lambda e: None, prefix="t")
        assert "运行模式" in msg.content or "模式" in msg.content

    def test_run_tool_call_ask_rejected(self, pm):
        import dot.graph.coding_graph as cg
        from dot.session import Session

        pm.set_approval_handler(lambda info: False)
        session = Session(session_id="perm-test2", workspace=pm._workspace)
        runtime = RuntimeState(workspace=pm._workspace, agent_mode="plan")
        msg = cg._run_tool_call(
            session, runtime, [],
            {"name": "FileWriteTool", "args": {"file_path": "src/a.py", "content": "x"}, "id": "c2"},
            lambda e: None, prefix="t",
        )
        assert "拒绝" in msg.content

    def test_run_tool_call_ask_approved_executes(self, pm):
        """ASK 批准后到达执行层（unknown tool 错误证明已放行）"""
        import dot.graph.coding_graph as cg
        from dot.session import Session

        pm.set_approval_handler(lambda info: True)
        session = Session(session_id="perm-test3", workspace=pm._workspace)
        runtime = RuntimeState(workspace=pm._workspace, agent_mode="plan")
        msg = cg._run_tool_call(
            session, runtime, [],
            {"name": "NoSuchTool", "args": {}, "id": "c3"},
            lambda e: None, prefix="t",
        )
        assert "unknown tool" in msg.content  # 不是权限拦截文案


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


# ============================================================
# MCP 官方 SDK 接入（doc/fix-mcp.md）
# ============================================================

class TestMcpSdkIntegration:
    def test_transport_inference(self):
        """配置 → 传输类型推断（高德 http / 百度 sse / 本地 stdio）"""
        from dot.mcp.manager import infer_transport_type

        assert infer_transport_type({"url": "https://mcp.amap.com/mcp?key=x"}) == "http"
        assert infer_transport_type({"url": "https://mcp.map.baidu.com/sse?ak=x"}) == "sse"
        assert infer_transport_type({"url": "https://x.com/sse"}) == "sse"
        assert infer_transport_type({"command": "node", "args": ["s.js"]}) == "stdio"
        assert infer_transport_type({"url": "https://x.com/mcp", "transportType": "sse"}) == "sse"
        assert infer_transport_type({}) == "stdio"

    def test_config_merge_and_discovery(self, tmp_path):
        """.dot/mcp.json 两级合并 + MCPHost 工具发现（无真实 server，空目录）"""
        from dot.mcp.manager import MCPManager
        from dot.mcp.host import MCPHost

        ws = tmp_path / "ws"
        (ws / ".dot").mkdir(parents=True)
        # 项目级配置一个不存在的 server → 连接失败但不抛异常
        (ws / ".dot" / "mcp.json").write_text(
            '{"mcpServers": {"dead-local": {"command": "no-such-bin-xyz", "args": []}}}',
            encoding="utf-8",
        )
        manager = MCPManager(workspace=ws)
        connected = manager.load_config_and_connect()
        assert connected == 0  # 连不上但不崩

        host = MCPHost(manager)
        tools = host.discover_tools()
        assert tools == []
        host.close()

    def test_sdk_client_transport_validation(self):
        """SdkClient 配置校验：缺 url/command 时 connect 报错"""
        import asyncio

        from dot.mcp.client import SdkClient

        client = SdkClient({"name": "bad", "transport_type": "http"})  # 缺 url
        with pytest.raises(ValueError):
            asyncio.run(client._open_transport())


# ============================================================
# 链路追踪（doc/fix-链路追踪.md）
# ============================================================

class TestTracing:
    def _trace_file(self, env) -> Path:
        import time as _time

        session = env.get_or_create_session()
        return env.workspace / ".dot" / "traces" / _time.strftime("%Y-%m-%d") / f"trace_{session.session_id}.jsonl"

    def _read_spans(self, env) -> list[dict]:
        from dot.trace import get_tracer

        get_tracer()._exporter.flush()
        path = self._trace_file(env)
        assert path.exists(), f"trace file not found: {path}"
        import json

        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_span_fields_and_trace_context(self, env):
        """一轮执行后：文件存在、字段齐全、trace_id 贯穿、parent 链正确"""
        _run(env, "trace me")
        spans = self._read_spans(env)

        assert len(spans) >= 5  # turn + 若干节点 + llm + persist

        required = {
            "trace_id", "span_id", "parent_span_id", "timestamp", "duration_ms",
            "service", "name", "status", "tags", "input_summary",
            "output_summary", "error_stack",
        }
        for span in spans:
            missing = required - set(span.keys())
            assert not missing, f"span {span.get('name')} missing fields: {missing}"
            assert span["tags"].get("session_id")

        # 同一轮的所有 span 共享 trace_id（mcp_connect 发生在 turn 之外，是独立的启动 trace）
        startup_names = {"mcp_connect"}
        turn = next(s for s in spans if s["name"] == "turn")
        in_turn = [s for s in spans if s["name"] not in startup_names]
        assert {s["trace_id"] for s in in_turn} == {turn["trace_id"]}
        assert turn["parent_span_id"] == ""
        node_names = {s["name"] for s in spans if s["service"] == "graph_node"}
        assert {"plan_node", "coding_agent", "finally_node"} <= node_names
        for s in spans:
            if s["service"] == "graph_node":
                assert s["parent_span_id"] == turn["span_id"]

        # 无 LLM 场景：llm span 记录了 error 状态与堆栈
        llm_errors = [s for s in spans if s["service"] == "llm" and s["status"] == "error"]
        assert llm_errors and all(s["error_stack"] for s in llm_errors)

        # 持久化 span 存在且带 git hash
        persist = [s for s in spans if s["name"] == "persist_turn"]
        assert persist and persist[0]["tags"].get("turn_id") == 1

    def test_resume_creates_new_trace(self, env):
        """resume 轮生成新 trace_id"""
        _run(env, "first")
        spans1 = self._read_spans(env)
        trace1 = {s["trace_id"] for s in spans1}

        for _ in env.resume_intervention("stop"):
            pass
        spans2 = self._read_spans(env)
        startup_names = {"mcp_connect"}
        trace1 = {s["trace_id"] for s in spans1 if s["name"] not in startup_names}
        trace2 = {s["trace_id"] for s in spans2 if s["name"] not in startup_names}
        assert len(trace1) == 1
        # 旧 turn trace + resume_turn 的新 trace
        assert len(trace2) == 2
        assert "resume_turn" in {s["name"] for s in spans2}

    def test_summary_truncated(self, env):
        """input/output 摘要截断，不存全量"""
        _run(env, "x" * 5000)
        spans = self._read_spans(env)
        turn = next(s for s in spans if s["name"] == "turn")
        assert len(turn["input_summary"]) <= 203  # 200 + "..."

    def test_disabled_tracer(self, tmp_path, no_llm, monkeypatch):
        """DOT_TRACE_ENABLED=0 时不产文件"""
        import sys

        monkeypatch.setenv("DOT_TRACE_ENABLED", "0")
        from dot.trace import init_tracer, reset_tracer, get_tracer

        reset_tracer()
        init_tracer(tmp_path)
        assert type(get_tracer()).__name__ == "NoopTracer"

        ws = tmp_path / "ws2"
        ws.mkdir()
        host = AgentHost(workspace=ws, sessions_root=tmp_path / "s2")
        _run(host, "no trace please")
        assert not (ws / ".dot" / "traces").exists()

        reset_tracer()  # 恢复全局，避免影响其他测试
        init_tracer(ws)  # 后续 fixture 会重新初始化
