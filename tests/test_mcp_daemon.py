"""Tests for MCP protocol, sandbox, transport, client, bridge, desktop pet, daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mokioclaw.mcp.protocol import (
    MCPTool,
    MCPToolResult,
    build_error_response,
    build_notification,
    build_request,
    build_response,
    extract_content_parts,
    is_notification,
    is_request,
    is_response,
    parse_message,
)
from mokioclaw.mcp.sandbox import (
    SandboxPolicy,
    _path_matches,
    permissive_policy,
    strict_policy,
    workspace_policy,
)
from mokioclaw.mcp.bridge import MCPBridge, get_mcp_bridge, reset_mcp_bridge
from mokioclaw.mcp.transport import MCPTransport
from mokioclaw.mcp.client import MCPClient
from mokioclaw.desktop.agent import DesktopPetAgent
from mokioclaw.daemon.manager import DaemonManager, DaemonInfo
from mokioclaw.daemon.scheduler import CronSchedule, CronScheduler, ScheduledTask
import datetime as dt


def _outside_workspace_path() -> str:
    """Return a path outside any reasonable workspace (platform-aware)."""
    if sys.platform == "win32":
        # Use current exe or system32 as outside path
        return str(Path(sys.executable).resolve())
    return "/etc/passwd"


def _make_echo_script() -> str:
    """Create a temp Python script that echoes JSON-RPC responses."""
    return r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        if msg.get("method") == "initialize":
            resp["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "echo", "version": "1.0"}
            }
        elif msg.get("method") == "tools/list":
            resp["result"] = {"tools": [{"name": "echo_tool", "description": "Echo", "inputSchema": {}}]}
        elif msg.get("method") == "tools/call":
            resp["result"] = {"content": [{"type": "text", "text": "echoed"}], "isError": False}
        elif msg.get("method") == "ping":
            resp["result"] = {}
        print(json.dumps(resp))
        sys.stdout.flush()
    except Exception:
        pass
'''


# ============================================================
# Protocol tests
# ============================================================

class TestProtocol:
    def test_build_request(self):
        msg = build_request("tools/call", {"name": "read"}, "req-1")
        assert msg == {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "tools/call",
            "params": {"name": "read"},
        }

    def test_build_request_no_params(self):
        msg = build_request("ping", None, "1")
        assert "params" not in msg

    def test_build_notification(self):
        msg = build_notification("notifications/initialized")
        assert msg == {"jsonrpc": "2.0", "method": "notifications/initialized"}

    def test_build_notification_with_params(self):
        msg = build_notification("test", {"key": "val"})
        assert msg["params"] == {"key": "val"}

    def test_build_response(self):
        msg = build_response({"tools": []}, "1")
        assert msg == {"jsonrpc": "2.0", "id": "1", "result": {"tools": []}}

    def test_build_error_response(self):
        msg = build_error_response("1", -32600, "Invalid Request")
        assert msg["error"]["code"] == -32600
        assert msg["error"]["message"] == "Invalid Request"

    def test_parse_message(self):
        raw = '{"jsonrpc": "2.0", "id": "1", "method": "test"}'
        msg = parse_message(raw)
        assert msg["method"] == "test"

    def test_parse_invalid_json(self):
        with pytest.raises(ValueError):
            parse_message("not json")

    def test_is_request(self):
        assert is_request({"jsonrpc": "2.0", "id": "1", "method": "test"})
        assert not is_request({"jsonrpc": "2.0", "result": {}})

    def test_is_response(self):
        assert is_response({"jsonrpc": "2.0", "id": "1", "result": {}})
        assert is_response({"jsonrpc": "2.0", "id": "1", "error": {}})

    def test_is_notification(self):
        assert is_notification({"jsonrpc": "2.0", "method": "test"})
        assert not is_notification({"jsonrpc": "2.0", "id": "1", "method": "test"})

    def test_extract_content_text(self):
        result = MCPToolResult(content=[
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ])
        text, attachments = extract_content_parts(result)
        assert "Hello" in text
        assert "World" in text
        assert attachments == []

    def test_extract_content_with_uri(self):
        result = MCPToolResult(content=[
            {"type": "resource", "uri": "file:///tmp/a.txt", "mimeType": "text/plain"},
        ])
        text, attachments = extract_content_parts(result)
        assert text == ""
        assert "file:///tmp/a.txt" in attachments

    def test_extract_from_dict(self):
        result = {"content": [{"type": "text", "text": "dict content"}]}
        text, _ = extract_content_parts(result)
        assert "dict content" in text


# ============================================================
# Sandbox tests
# ============================================================

class TestSandbox:
    def test_workspace_policy_allows_inside(self):
        ws = Path(tempfile.mkdtemp())
        policy = workspace_policy(ws)
        test_file = str(ws / "test.py")
        allowed, _ = policy.check_file_access(test_file)
        assert allowed, f"Should allow {test_file} inside workspace"

    def test_workspace_policy_denies_outside(self):
        ws = Path(tempfile.mkdtemp())
        policy = workspace_policy(ws)
        outside = _outside_workspace_path()
        allowed, reason = policy.check_file_access(outside)
        assert not allowed, f"Should deny {outside} outside workspace: {reason}"

    def test_workspace_policy_allows_subdirs(self):
        ws = Path(tempfile.mkdtemp())
        subdir = ws / "src"
        subdir.mkdir(parents=True, exist_ok=True)
        test_file = subdir / "main.py"
        test_file.write_text("x", encoding="utf-8")
        policy = workspace_policy(ws)
        allowed, _ = policy.check_file_access(str(test_file))
        assert allowed

    def test_custom_denied_paths(self):
        outside = _outside_workspace_path()
        policy = SandboxPolicy(denied_paths=[outside])
        allowed, reason = policy.check_file_access(outside)
        assert not allowed, f"Should deny {outside}: {reason}"

    def test_custom_allowed_paths(self):
        policy = SandboxPolicy(allowed_paths=["/allowed/*"])
        allowed, _ = policy.check_file_access("/allowed/file.txt")
        assert allowed
        allowed2, _ = policy.check_file_access("/denied/file.txt")
        assert not allowed2

    def test_strict_policy_with_denylist(self):
        policy = strict_policy()
        outside = _outside_workspace_path()
        policy.denied_paths = [outside]
        assert not policy.check_file_access(outside)[0]

    def test_permissive_policy(self):
        policy = permissive_policy()
        assert policy.allow_network
        assert policy.max_execution_seconds == 60
        assert policy.max_output_chars == 50000

    def test_command_check_allowed(self):
        policy = SandboxPolicy(allowed_commands=["python*", "cat"])
        allowed, _ = policy.check_command("python script.py")
        assert allowed

    def test_command_check_denied(self):
        policy = SandboxPolicy()
        policy.denied_commands = ["rm"]
        allowed, _ = policy.check_command("rm -rf /")
        assert not allowed

    def test_network_check_permissive(self):
        policy = permissive_policy()
        allowed, _ = policy.check_network()
        assert allowed

    def test_network_check_default(self):
        policy = SandboxPolicy()
        assert not policy.allow_network

    def test_path_matches_glob(self):
        assert _path_matches("/etc/passwd", "/etc/*")
        assert _path_matches("/tmp/workspace/src/main.py", "/tmp/workspace/*")

    def test_path_matches_prefix(self):
        assert _path_matches("/tmp/workspace/src/main.py", "/tmp/workspace/")


# ============================================================
# Transport tests
# ============================================================

class TestTransport:
    def test_connect_disconnect(self):
        script = _make_echo_script()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(script)
            script_path = f.name
        try:
            transport = MCPTransport(command=sys.executable, args=[script_path])
            transport.connect()
            assert transport.is_connected()
            transport.disconnect()
            assert not transport.is_connected()
        finally:
            os.unlink(script_path)

    def test_send_receive_roundtrip(self):
        script = _make_echo_script()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(script)
            script_path = f.name
        try:
            transport = MCPTransport(command=sys.executable, args=[script_path])
            transport.connect()
            msg = {"jsonrpc": "2.0", "id": "1", "method": "ping", "params": {}}
            transport.send(msg)
            response = transport.receive(timeout=5.0)
            assert response is not None
            # transport.receive() already parses JSON, returns dict
            assert response["id"] == "1"
            assert "result" in response
            transport.disconnect()
        finally:
            os.unlink(script_path)


# ============================================================
# Client tests
# ============================================================

class TestMCPClient:
    def _make_client(self):
        script = _make_echo_script()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(script)
            script_path = f.name
        transport = MCPTransport(command=sys.executable, args=[script_path])
        return transport, script_path

    def test_client_initialize_and_list_tools(self):
        transport, script_path = self._make_client()
        try:
            client = MCPClient(name="echo", transport=transport)
            assert client.connect()
            assert client.state.value == "connected"
            assert client.server_info["name"] == "echo"
            tools = client.list_tools()
            assert len(tools) == 1
            assert tools[0].name == "echo_tool"
            client.disconnect()
            assert client.state.value == "disconnected"
        finally:
            os.unlink(script_path)

    def test_client_call_tool(self):
        transport, script_path = self._make_client()
        try:
            client = MCPClient(name="echo", transport=transport)
            assert client.connect()
            result = client.call_tool("echo_tool", {})
            assert not result.is_error
            text, _ = extract_content_parts(result)
            assert "echoed" in text
            client.disconnect()
        finally:
            os.unlink(script_path)


# ============================================================
# Bridge tests
# ============================================================

class TestMCPBridge:
    def setup_method(self):
        reset_mcp_bridge()

    def teardown_method(self):
        reset_mcp_bridge()

    def test_empty_bridge(self):
        bridge = MCPBridge()
        assert bridge.list_servers() == []
        assert bridge.to_langchain_tools() == []

    def test_register_server_fails_gracefully(self):
        bridge = MCPBridge()
        result = bridge.register_server("test", command="nonexistent_cmd_xyz")
        assert result is False

    def test_call_tool_no_server(self):
        bridge = MCPBridge()
        result = bridge.call_tool("server:tool", {})
        assert result["ok"] is False
        assert "not connected" in result["error"]

    def test_to_dict_empty(self):
        bridge = MCPBridge()
        assert bridge.to_dict() == {}

    def test_singleton(self):
        b1 = get_mcp_bridge()
        b2 = get_mcp_bridge()
        assert b1 is b2
        reset_mcp_bridge()


# ============================================================
# Desktop pet tests
# ============================================================

class TestDesktopPet:
    def test_status_defaults(self):
        from mokioclaw.desktop.agent import StatusSnapshot
        snap = StatusSnapshot()
        assert snap.agent_status == "idle"
        assert snap.error_count == 0

    def test_status_to_dict(self):
        from mokioclaw.desktop.agent import StatusSnapshot
        snap = StatusSnapshot(agent_status="thinking", current_task="test")
        d = snap.to_dict()
        assert d["agent_status"] == "thinking"
        assert d["current_task"] == "test"

    def test_pet_creation(self):
        with tempfile.TemporaryDirectory() as td:
            pet = DesktopPetAgent(workspace=Path(td))
            assert pet.status.agent_status == "idle"

    def test_pet_notify_no_raise(self):
        with tempfile.TemporaryDirectory() as td:
            pet = DesktopPetAgent(workspace=Path(td))
            pet.notify("test", "msg", "info")

    def test_pet_status_file_path(self):
        with tempfile.TemporaryDirectory() as td:
            pet = DesktopPetAgent(workspace=Path(td))
            sf = pet.get_status_file()
            assert sf is not None
            assert ".mokioclaw" in str(sf)


# ============================================================
# Daemon manager tests
# ============================================================

class TestDaemonManager:
    def test_not_running_initially(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = DaemonManager(workspace=Path(td))
            assert not mgr.is_running()
            info = mgr.get_info()
            assert info.status == "stopped"

    def test_start_and_stop(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = DaemonManager(workspace=Path(td))
            cmd = [sys.executable, "-c", "import time; time.sleep(0.5)"]
            info = mgr.start(cmd)
            assert info.pid > 0
            assert info.status == "running"
            time.sleep(0.2)
            assert mgr.is_running()
            mgr.stop()
            assert not mgr.is_running()

    def test_daemon_dir_created(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = DaemonManager(workspace=Path(td))
            daemon_dir = mgr._daemon_dir
            assert daemon_dir.exists()

    def test_get_info_structure(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = DaemonManager(workspace=Path(td))
            info = mgr.get_info()
            assert isinstance(info, DaemonInfo)
            assert "status" in info.to_dict()


# ============================================================
# Cron scheduler tests
# ============================================================

class TestCronScheduler:
    def test_add_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            scheduler = CronScheduler(tasks_dir=Path(td))
            task = ScheduledTask(name="test-job", cron="0 9 * * *", command="echo hi")
            task_id = scheduler.add_task(task)
            assert task_id != ""
            tasks = scheduler.tasks
            assert len(tasks) == 1
            assert tasks[0].name == "test-job"

    def test_remove_task(self):
        with tempfile.TemporaryDirectory() as td:
            scheduler = CronScheduler(tasks_dir=Path(td))
            task = ScheduledTask(name="to-remove", cron="0 0 * * *", command="true")
            task_id = scheduler.add_task(task)
            assert scheduler.remove_task(task_id)
            assert len(scheduler.tasks) == 0

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            s1 = CronScheduler(tasks_dir=Path(td))
            s1.add_task(ScheduledTask(name="persist-test", cron="*/5 * * * *", command="echo 1"))
            s2 = CronScheduler(tasks_dir=Path(td))
            tasks = s2.tasks
            assert len(tasks) == 1
            assert tasks[0].name == "persist-test"
            assert tasks[0].cron == "*/5 * * * *"

    def test_cron_matches_weekdays(self):
        sched = CronSchedule("0 9 * * 1-5")
        assert sched.matches(dt.datetime(2024, 1, 1, 9, 0))   # Monday
        assert not sched.matches(dt.datetime(2024, 1, 6, 9, 0))  # Saturday
        assert not sched.matches(dt.datetime(2024, 1, 7, 9, 0))  # Sunday
        assert not sched.matches(dt.datetime(2024, 1, 1, 10, 0))  # Wrong time

    def test_cron_step(self):
        sched = CronSchedule("*/15 * * * *")
        assert sched.matches(dt.datetime(2024, 1, 1, 0, 0))
        assert sched.matches(dt.datetime(2024, 1, 1, 0, 15))
        assert not sched.matches(dt.datetime(2024, 1, 1, 0, 7))

    def test_cron_invalid_expression(self):
        with pytest.raises(ValueError):
            CronSchedule("invalid")

    def test_cron_range(self):
        sched = CronSchedule("0 8-10 * * *")
        assert sched.matches(dt.datetime(2024, 1, 1, 8, 0))
        assert sched.matches(dt.datetime(2024, 1, 1, 10, 0))
        assert not sched.matches(dt.datetime(2024, 1, 1, 7, 0))
        assert not sched.matches(dt.datetime(2024, 1, 1, 11, 0))

    def test_cron_month_range(self):
        sched = CronSchedule("0 0 1 1,6,12 *")
        assert sched.matches(dt.datetime(2024, 1, 1, 0, 0))
        assert sched.matches(dt.datetime(2024, 6, 1, 0, 0))
        assert not sched.matches(dt.datetime(2024, 3, 1, 0, 0))
