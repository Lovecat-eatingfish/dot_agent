"""
dot.coding.webui.bridge — WebUI 环境审批桥（零框架依赖）

对应权限设计文档（doc/fix-权限控制.md §10.4.1）的 WebUI 环境链路：
  权限 ASK → 挂起 asyncio Future + 缓存待审批请求 → 前端经 HTTP 拉取/响应
  → Future 完成 → agent 恢复执行（批准后重走完整三级校验）。

fail-closed：超时（可选）或未知 request_id 一律按拒绝处理，与无头兜底一致。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApprovalRequest:
    """一条待人工审批的权限请求"""
    request_id: str
    info: dict[str, Any]
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop


class WebUIApprovalBridge:
    """把权限 ASK 转成可经 HTTP 响应的待审批请求

    用法（WebUI server 启动时）：
        host.webui_bridge.pending()      # GET 给前端
        host.webui_bridge.respond(id, approved)   # 前端点击后 POST
        host.permission.set_approval_handler(host.webui_bridge.handler)
    """

    def __init__(
        self,
        *,
        timeout: float | None = None,
        on_accept_all: Callable[[], None] | None = None,
    ) -> None:
        self._timeout = timeout          # None = 挂起等待前端响应（设计文档默认形态）
        self._on_accept_all = on_accept_all
        self._pending: dict[str, ApprovalRequest] = {}

    # ============================================================
    # agent 侧：ask_user 调用的 handler
    # ============================================================

    async def handler(self, info: dict) -> bool:
        loop = asyncio.get_running_loop()
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = ApprovalRequest(
            request_id=request_id, info=info, future=future, loop=loop,
        )
        logger.info("[webui] approval requested: %s (%s)",
                    request_id, info.get("tool_name", ""))
        try:
            if self._timeout is None:
                return await future
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            logger.info("[webui] approval %s timed out, auto-deny", request_id)
            return False
        finally:
            self._pending.pop(request_id, None)

    # ============================================================
    # HTTP 侧：拉取与响应
    # ============================================================

    def pending(self) -> list[dict[str, Any]]:
        """当前待审批请求（request_id + 原始审批信息含 diff 预览）"""
        return [
            {"request_id": r.request_id, "info": r.info}
            for r in self._pending.values()
        ]

    def respond(self, request_id: str, approved: bool, *, accept_all: bool = False) -> bool:
        """响应一条审批；返回 False 表示请求不存在或已处理

        accept_all=True 时（前端"本会话全部接受"按钮）同时开启会话级豁免。
        可能从 uvicorn 工作线程调用，跨线程用 call_soon_threadsafe 唤醒。
        """
        request = self._pending.get(request_id)
        if request is None or request.future.done():
            return False
        if approved and accept_all and self._on_accept_all is not None:
            try:
                self._on_accept_all()
            except Exception:
                logger.exception("[webui] on_accept_all failed")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is request.loop:
            _set_result_safe(request, bool(approved))
        else:
            request.loop.call_soon_threadsafe(_set_result_safe, request, bool(approved))
        return True


def _set_result_safe(request: ApprovalRequest, approved: bool) -> None:
    if not request.future.done():
        request.future.set_result(approved)
