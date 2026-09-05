"""review 修复回归测试：配对清洗 / 沙箱穿越 / marketplace 边界 / daemon 存活检查 / 补触发"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


class TestPairingSafeCompaction:
    """#3：所有 messages[-keep_last:] 切片出口过配对清洗，孤儿 ToolMessage 必须被丢弃"""

    def _messages(self) -> list:
        # 构造尾部以孤儿 ToolMessage 开头的列表
        return [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            AIMessage(content="a1", tool_calls=[{"name": "t", "args": {}, "id": "call-1"}]),
            ToolMessage(content="r1", tool_call_id="call-1"),
            HumanMessage(content="q2"),
        ]

    def test_reactive_compact_drops_orphan_tool_message(self) -> None:
        from mokioclaw.memory.snip import reactive_compact_messages

        msgs = self._messages()
        # keep_last=1 → 尾部只有 ToolMessage(r1)，其父 AIMessage 被切掉
        out = reactive_compact_messages(msgs, keep_last=1)
        tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
        assert tool_msgs == [], "孤儿 ToolMessage 必须被丢弃，否则 API 400"

    def test_force_compact_drops_orphan_tool_message(self) -> None:
        from mokioclaw.memory.microcompact import force_compact_messages

        msgs = self._messages()
        out = force_compact_messages(msgs, keep_last=1)
        tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
        assert tool_msgs == []

    def test_valid_pairing_preserved(self) -> None:
        from mokioclaw.memory.snip import reactive_compact_messages

        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            AIMessage(content="a1", tool_calls=[{"name": "t", "args": {}, "id": "call-1"}]),
            ToolMessage(content="r1", tool_call_id="call-1"),
        ]
        out = reactive_compact_messages(msgs, keep_last=3)
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call-1" for m in out)

    def test_make_pairing_safe_fills_dangling_tool_call(self) -> None:
        from mokioclaw.reliability.token_budget import make_pairing_safe

        # 末尾 AIMessage 带 tool_call 但无 result → 补占位
        msgs = [AIMessage(content="a", tool_calls=[{"name": "t", "args": {}, "id": "call-x"}])]
        out = make_pairing_safe(msgs)
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call-x" for m in out)


class TestSandboxRelativeTraversal:
    """#6：相对路径穿越（../）不再放行"""

    def _state(self, tmp_path: Path):
        from mokioclaw.state.runtime import RuntimeState

        return RuntimeState(workspace=tmp_path, approval_mode="auto")

    def test_dotdot_slash_blocked(self, tmp_path: Path) -> None:
        from mokioclaw.security.sandbox import check_sandbox

        reason = check_sandbox(self._state(tmp_path), "del ../../secret.txt")
        assert reason is not None and "traversal" in reason

    def test_backslash_dotdot_blocked(self, tmp_path: Path) -> None:
        from mokioclaw.security.sandbox import check_sandbox

        reason = check_sandbox(self._state(tmp_path), "type ..\\..\\config")
        assert reason is not None

    def test_bare_dotdot_word_blocked(self, tmp_path: Path) -> None:
        from mokioclaw.security.sandbox import check_sandbox

        reason = check_sandbox(self._state(tmp_path), "cd .. && dir")
        assert reason is not None

    def test_normal_relative_path_allowed(self, tmp_path: Path) -> None:
        from mokioclaw.security.sandbox import check_sandbox

        assert check_sandbox(self._state(tmp_path), "python tests/test_x.py") is None

    def test_git_range_not_blocked(self, tmp_path: Path) -> None:
        """git log HEAD~2..HEAD 的 .. 不应误伤"""
        from mokioclaw.security.sandbox import check_sandbox

        assert check_sandbox(self._state(tmp_path), "git log --oneline HEAD~2..HEAD") is None


class TestMarketplacePathBoundary:
    """#8：workspace 级 catalog 不允许指向外部目录"""

    def test_untrusted_catalog_outside_path_ignored(self, tmp_path: Path) -> None:
        from mokioclaw.plugins import marketplace

        catalog = tmp_path / ".mokioclaw" / "marketplace" / "catalog.json"
        catalog.parent.mkdir(parents=True)
        import json

        catalog.write_text(
            json.dumps([{"name": "evil", "path": str(Path.home() / ".ssh")}]),
            encoding="utf-8",
        )
        names = [p.name for p in marketplace.list_catalog(tmp_path)]
        assert "evil" not in names

    def test_workspace_inside_path_allowed(self, tmp_path: Path) -> None:
        from mokioclaw.plugins import marketplace

        plugin_dir = tmp_path / "myplugins" / "good"
        plugin_dir.mkdir(parents=True)
        import json

        catalog = tmp_path / ".mokioclaw" / "marketplace" / "catalog.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(
            json.dumps([{"name": "good", "path": str(plugin_dir)}]),
            encoding="utf-8",
        )
        names = [p.name for p in marketplace.list_catalog(tmp_path)]
        assert "good" in names

    def test_is_allowed_plugin_source(self, tmp_path: Path) -> None:
        from mokioclaw.plugins.marketplace import _is_allowed_plugin_source

        assert _is_allowed_plugin_source(None, tmp_path) is True
        assert _is_allowed_plugin_source(tmp_path / "sub" / "p", tmp_path) is True
        assert _is_allowed_plugin_source(Path.home() / ".ssh", tmp_path) is False


class TestDaemonAliveCheck:
    """#10：Windows 上 _is_process_alive 不再恒为 True"""

    def test_dead_process_not_alive(self) -> None:
        import subprocess
        import sys
        import time

        from mokioclaw.daemon.manager import _is_process_alive

        proc = subprocess.Popen(
            [sys.executable, "-c", "print('bye')"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        time.sleep(0.2)
        assert _is_process_alive(proc.pid) is False, "已退出进程不应被判定为存活"

    def test_live_process_alive(self) -> None:
        import subprocess
        import sys

        from mokioclaw.daemon.manager import _is_process_alive

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _is_process_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()


class TestSchedulerCatchUp:
    """#11：基于 last_triggered 的补触发"""

    def test_catch_up_fires_missed_trigger(self, tmp_path: Path) -> None:
        import threading

        from mokioclaw.daemon.scheduler import CronScheduler, ScheduledTask

        scheduler = CronScheduler(tasks_dir=tmp_path / "tasks", check_interval=60)
        scheduler._on_task_run = lambda task: None

        task = ScheduledTask(name="t1", cron="*/5 * * * *", command="echo hi")
        scheduler.add_task(task)
        # 模拟上一次触发在 10 分钟前 → 本应 5 分钟前再触发，采样漂移漏掉了
        task.last_triggered = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

        now = datetime.now(timezone.utc)
        scheduler._check_triggers(now)
        assert task.run_count == 1, "漏掉的触发点应被补跑"

        # 补跑后基线推进，同一时刻重复检查不再触发
        scheduler._check_triggers(now + timedelta(seconds=1))
        assert task.run_count == 1

    def test_next_run_computes(self) -> None:
        from mokioclaw.daemon.scheduler import CronSchedule

        schedule = CronSchedule("30 9 * * *")
        after = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
        nxt = schedule.next_run(after)
        assert nxt is not None and nxt.hour == 9 and nxt.minute == 30 and nxt.day == 17


class TestTopicStoreSharedLock:
    """#2：同一 workspace 的 TopicStore 实例共享写锁"""

    def test_instances_share_lock(self, tmp_path: Path) -> None:
        from mokioclaw.memory.topic_store import TopicStore

        s1 = TopicStore(tmp_path)
        s2 = TopicStore(tmp_path / "sub" / "..")
        assert s1._write_lock is s2._write_lock

    def test_different_workspace_distinct_lock(self, tmp_path: Path) -> None:
        from mokioclaw.memory.topic_store import TopicStore

        assert TopicStore(tmp_path)._write_lock is not TopicStore(tmp_path / "other")._write_lock

    def test_concurrent_write_no_lost_index(self, tmp_path: Path) -> None:
        import threading

        from mokioclaw.memory.topic_store import TopicStore

        store = TopicStore(tmp_path)
        store.ensure_dir()
        barrier = threading.Barrier(8, timeout=10)

        def writer(i: int) -> None:
            barrier.wait()
            for j in range(10):
                TopicStore(tmp_path).write_topic(
                    f"topic_{i}_{j}", f"content {i}-{j}", topic_type="project", description=f"d{i}-{j}"
                )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        index = TopicStore(tmp_path).load_index()
        # 80 个并发写入，索引条目一个都不能丢
        for i in range(8):
            for j in range(10):
                assert f"topic_{i}_{j}" in index, f"lost index entry: topic_{i}_{j}"


class TestMCPClientCallLock:
    """#4：MCP client 请求-响应周期串行"""

    def test_call_tool_holds_lock(self) -> None:
        from mokioclaw.mcp.client import MCPClient

        client = MCPClient.__new__(MCPClient)
        import threading

        client._call_lock = threading.Lock()
        # call_tool 在拿锁前会先做沙箱检查（policy=None 跳过），然后尝试拿锁
        acquired = []

        def blocked_call() -> None:
            client._policy = None
            import pytest

            with pytest.raises(Exception):
                client.call_tool("x", {})  # transport 未初始化会抛错，但锁必须先被持有

        client._call_lock.acquire()
        try:
            # 持锁状态下另一线程调 call_tool：应阻塞在锁上而非立刻失败
            t = threading.Thread(target=blocked_call, daemon=True)
            t.start()
            t.join(0.3)
            assert t.is_alive(), "call_tool 应阻塞在 _call_lock 上"
            acquired.append(True)
        finally:
            client._call_lock.release()


class TestNotifyEscaping:
    """#1：macOS 通知的 AppleScript 转义"""

    def test_escape_quotes_and_backslashes(self) -> None:
        from mokioclaw.desktop.agent import _applescript_escape

        assert _applescript_escape('say "hello"') == 'say \\"hello\\"'
        assert _applescript_escape("back\\slash") == "back\\\\slash"
        assert _applescript_escape('do shell script "rm -rf /"') == 'do shell script \\"rm -rf /\\"'

    def test_notify_macos_builds_escaped_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mokioclaw.desktop.agent as desktop_agent

        captured: dict[str, str] = {}

        def fake_run(args, *a, **kw):  # type: ignore[no-untyped-def]
            captured["script"] = args[2]
            return None

        monkeypatch.setattr("subprocess.run", fake_run)
        desktop_agent._notify_macos('ti"tle', 'mes"sage')
        script = captured["script"]
        # 消息里的引号必须以 \" 形式出现，不能裸引号
        assert 'mes\\"sage' in script
        assert 'ti\\"tle' in script
        # 裸引号只允许出现在 AppleScript 语法边界（成对包裹转义后的内容）
        assert script.count('"') == script.count('\\"') + 6  # display/title/sound 三对语法引号
