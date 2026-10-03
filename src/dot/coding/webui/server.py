"""
dot.coding.webui.server — WebUI 审批桥服务装配

create_app(host) 返回 FastAPI 应用（/api/agent/* 路由 + health），
serve(host) 注册审批桥为 approval handler 并用 uvicorn 拉起服务。

前端渲染层（浏览器页面）待 WebUI 立项后补充；本模块提供完整可用的
服务端闭环：prompt → ASK 挂起 → HTTP 审批 → 恢复 → 事后评审。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from dot.coding.host import CodingHost


def create_app(host: "CodingHost") -> "FastAPI":
    """装配 FastAPI 应用（不含静态前端页面）"""
    from fastapi import FastAPI

    from .routes import create_review_router

    app = FastAPI(title="dot agent review bridge", version="0.1.0")
    app.include_router(create_review_router(host))

    @app.get("/api/agent/health")
    def health() -> dict:
        return {
            "status": "ok",
            "session": host.session_id,
            "mode": host.mode.value,
            "pending_changes": host.review.pending_count,
        }

    return app


def serve(
    host: "CodingHost",
    *,
    bind_host: str = "127.0.0.1",
    port: int = 8002,
) -> None:
    """注册审批桥并阻塞式启动服务（交互式 WebUI 环境，关闭自动接受）"""
    host.review.auto_resolve = False
    host.permission.set_approval_handler(host.webui_bridge.handler)

    import uvicorn

    uvicorn.run(create_app(host), host=bind_host, port=port, log_level="info")
