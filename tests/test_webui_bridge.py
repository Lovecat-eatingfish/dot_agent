"""WebUI 审批桥测试：bridge 单元（挂起/响应/超时/跨线程）+ FastAPI 完整闭环"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from dot.coding.modes import AgentMode
from dot.coding.webui.bridge import WebUIApprovalBridge
from test_review_e2e import ScriptedProvider, _write_call


def _info(tool: str = "write_file") -> dict:
    return {
        "tool_name": tool, "agent_mode": "edit", "source": "mode",
        "reason": "edit mode: file write needs review", "args": {"file_path": "a.py"},
        "preview": {"kind": "create", "path": "a.py", "diff": "+x", "truncated": False,
                    "error": "", "lines_added": 1, "lines_removed": 0},
    }


# ============================================================
# bridge 单元
# ============================================================

def test_bridge_respond_resolves_handler() -> None:
    bridge = WebUIApprovalBridge()

    async def _run() -> None:
        task = asyncio.ensure_future(bridge.handler(_info()))
        await asyncio.sleep(0.05)
        requests = bridge.pending()
        assert len(requests) == 1
        assert requests[0]["info"]["tool_name"] == "write_file"
        assert bridge.respond(requests[0]["request_id"], True) is True
        assert await task is True
        assert bridge.pending() == []

    asyncio.run(_run())


def test_bridge_respond_deny() -> None:
    bridge = WebUIApprovalBridge()

    async def _run() -> None:
        task = asyncio.ensure_future(bridge.handler(_info()))
        await asyncio.sleep(0.05)
        request_id = bridge.pending()[0]["request_id"]
        assert bridge.respond(request_id, False) is True
        assert await task is False

    asyncio.run(_run())


def test_bridge_unknown_request_id() -> None:
    assert WebUIApprovalBridge().respond("nope", True) is False


def test_bridge_timeout_auto_denies() -> None:
    bridge = WebUIApprovalBridge(timeout=0.05)

    async def _run() -> None:
        assert await bridge.handler(_info()) is False  # 超时 → fail-closed 拒绝
        assert bridge.pending() == []

    asyncio.run(_run())


def test_bridge_accept_all_enables_callback_only_when_approved() -> None:
    enabled = {"flag": False}

    def on_accept_all() -> None:
        enabled["flag"] = True

    bridge = WebUIApprovalBridge(on_accept_all=on_accept_all)

    async def _run() -> None:
        task = asyncio.ensure_future(bridge.handler(_info()))
        await asyncio.sleep(0.05)
        request_id = bridge.pending()[0]["request_id"]
        # approved=False 时即使 accept_all=True 也不开启豁免
        bridge.respond(request_id, False, accept_all=True)
        await task
        assert enabled["flag"] is False

        task = asyncio.ensure_future(bridge.handler(_info()))
        await asyncio.sleep(0.05)
        request_id = bridge.pending()[0]["request_id"]
        bridge.respond(request_id, True, accept_all=True)
        await task
        assert enabled["flag"] is True

    asyncio.run(_run())


def test_bridge_respond_from_other_thread() -> None:
    """uvicorn 工作线程响应 → call_soon_threadsafe 唤醒 agent 侧 Future"""
    bridge = WebUIApprovalBridge()

    async def _run() -> None:
        task = asyncio.ensure_future(bridge.handler(_info()))
        await asyncio.sleep(0.05)
        request_id = bridge.pending()[0]["request_id"]
        thread = threading.Thread(
            target=bridge.respond, args=(request_id, True), daemon=True,
        )
        thread.start()
        thread.join(timeout=5)
        assert await task is True

    asyncio.run(_run())


# ============================================================
# FastAPI 完整闭环：prompt → ASK 挂起 → HTTP 审批 → 恢复 → 事后评审
# ============================================================

def test_webui_full_approval_flow(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    asyncio.run(_full_flow(tmp_path))


async def _full_flow(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    from dot.coding.host import CodingHost
    from dot.coding.permission import reset_permission_manager
    from dot.coding.webui.server import create_app

    reset_permission_manager()
    host = CodingHost(workspace=tmp_path, mode=AgentMode.EDIT)  # EDIT：写操作走 ASK
    harness = host.create_harness(system="t")
    harness._config.provider = ScriptedProvider(
        [[_write_call("c1", "web.py", "x = 1\n")], "done"],
    )
    host.review.auto_resolve = False
    host.permission.set_approval_handler(host.webui_bridge.handler)  # serve() 的接线

    app = create_app(host)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        health = await client.get("/api/agent/health")
        assert health.json()["status"] == "ok"

        # prompt 发起 → ASK 挂起在审批桥
        prompt_task = asyncio.ensure_future(
            client.post("/api/agent/prompt", json={"text": "write"}),
        )
        requests: list[dict] = []
        for _ in range(100):
            await asyncio.sleep(0.05)
            response = await client.get("/api/agent/approvals")
            requests = response.json()["requests"]
            if requests:
                break
        assert len(requests) == 1
        assert requests[0]["info"]["tool_name"] == "write_file"
        assert requests[0]["info"]["preview"]["kind"] == "create"  # 审批信息带 diff 预览
        assert not (tmp_path / "web.py").exists()  # 挂起期间未落盘

        # 前端批准 → agent 恢复执行
        response = await client.post(
            f"/api/agent/approvals/{requests[0]['request_id']}",
            json={"approved": True},
        )
        assert response.json()["ok"] is True
        result = await prompt_task
        assert result.status_code == 200
        assert result.json()["pending_changes"] == 1
        assert (tmp_path / "web.py").read_text(encoding="utf-8") == "x = 1\n"

        # 事后评审 API：查看 → 回滚（回滚通知注入 agent 历史）
        response = await client.get("/api/agent/review/pending")
        payload = response.json()
        assert payload["pending_count"] == 1
        assert payload["records"][0]["path"] == "web.py"
        assert "before_text" not in payload["records"][0]  # 快照内容不外泄

        response = await client.post("/api/agent/review/revert", json={"target": "all"})
        assert "reverted" in response.json()["result"]
        assert not (tmp_path / "web.py").exists()
        assert any("已回滚" in getattr(m, "text", "")
                   for m in host._harness.messages)


def test_webui_reject_flow_keeps_file_and_notifies(tmp_path: Path) -> None:
    """HTTP 拒绝：文件不落盘，agent 收到 'user rejected the change'"""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    asyncio.run(_reject_flow(tmp_path))


async def _reject_flow(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    from dot.coding.host import CodingHost
    from dot.coding.permission import reset_permission_manager
    from dot.coding.webui.server import create_app

    reset_permission_manager()
    host = CodingHost(workspace=tmp_path, mode=AgentMode.EDIT)
    harness = host.create_harness(system="t")
    harness._config.provider = ScriptedProvider(
        [[_write_call("c1", "denied.py", "x = 1\n")], "done"],
    )
    host.permission.set_approval_handler(host.webui_bridge.handler)
    app = create_app(host)

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        prompt_task = asyncio.ensure_future(
            client.post("/api/agent/prompt", json={"text": "write"}),
        )
        for _ in range(100):
            await asyncio.sleep(0.05)
            response = await client.get("/api/agent/approvals")
            requests = response.json()["requests"]
            if requests:
                break
        assert len(requests) == 1

        response = await client.post(
            f"/api/agent/approvals/{requests[0]['request_id']}",
            json={"approved": False},  # 前端点"拒绝"
        )
        assert response.json()["ok"] is True

        result = await prompt_task
        assert result.status_code == 200
        assert result.json()["pending_changes"] == 0  # 被拒调用不进台账
        assert not (tmp_path / "denied.py").exists()

        # 从 harness 历史确认拒绝原因已返回给 agent
        from dot.ai.types import ToolResultMessage

        rejects = [
            m for m in host._harness.messages
            if isinstance(m, ToolResultMessage)
            and "user rejected the change" in "".join(
                block.text for block in m.content if hasattr(block, "text")
            )
        ]
        assert len(rejects) == 1


def test_webui_respond_unknown_returns_404(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    asyncio.run(_unknown_404(tmp_path))


async def _unknown_404(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    from dot.coding.host import CodingHost
    from dot.coding.permission import reset_permission_manager
    from dot.coding.webui.server import create_app

    reset_permission_manager()
    host = CodingHost(workspace=tmp_path, mode=AgentMode.AUTO)
    host.create_harness(system="t")
    app = create_app(host)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        response = await client.post(
            "/api/agent/approvals/deadbeef", json={"approved": True},
        )
        assert response.status_code == 404
