"""
dot.coding.webui.routes — WebUI 审批桥的 FastAPI 路由

前端（未来）对接面：
- 对话          ：POST /api/agent/prompt（阻塞到 turn 结束，返回回复与 pending 数）
- 事前审批      ：GET  /api/agent/approvals → POST /api/agent/approvals/{id}
- 事后评审      ：GET  /api/agent/review/pending → POST /api/agent/review/accept|revert

fastapi 延迟导入：不启 WebUI 时零开销。

注意：本模块不能加 `from __future__ import annotations`——FastAPI 对嵌套
定义的 Pydantic 模型做 get_type_hints 解析时无法还原字符串注解，
会把 body 参数误判成 query 参数（422）。
"""

from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dot.coding.host import CodingHost

_RECORD_DICT_FIELDS = (
    "id", "tool_call_id", "tool_name", "path", "kind", "turn_id",
    "existed_before", "snapshot_bytes", "pre_commit", "fallback_git",
    "lines_added", "lines_removed", "status",
)


def _record_dict(record) -> dict:
    data = asdict(record) if is_dataclass(record) else vars(record)
    return {field: data[field] for field in _RECORD_DICT_FIELDS}


def create_review_router(host: "CodingHost"):
    """构建挂载到 FastAPI 应用的审批/评审路由"""
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    router = APIRouter(prefix="/api/agent")

    class ReviewAction(BaseModel):
        target: str = "all"          # 具体 id（"3" / "#3"）或 "all"

    class ApprovalResponse(BaseModel):
        approved: bool
        accept_all: bool = False     # 前端"本会话全部接受"按钮

    class PromptRequest(BaseModel):
        text: str

    # ---- 对话（驱动一轮 agent turn）----

    @router.post("/prompt")
    async def prompt(body: PromptRequest) -> dict:
        harness = host._harness
        if harness is None:
            harness = host.create_harness(system="You are a helpful coding assistant.")
        if harness.is_running:
            raise HTTPException(status_code=409, detail="agent is running")
        from dot.agent.events import MessageEndEvent
        from dot.ai.types import AssistantMessage

        texts: list[str] = []
        async for event in harness.prompt(body.text):
            if isinstance(event, MessageEndEvent) and isinstance(
                event.message, AssistantMessage,
            ):
                text = event.message.text
                if text:
                    texts.append(text)
        host.end_turn()
        return {
            "text": "\n".join(texts),
            "pending_changes": host.review.pending_count,
        }

    # ---- 事前审批（ASK 挂起/恢复）----

    @router.get("/approvals")
    def pending_approvals() -> dict:
        return {"requests": host.webui_bridge.pending()}

    @router.post("/approvals/{request_id}")
    def respond_approval(request_id: str, body: ApprovalResponse) -> dict:
        ok = host.webui_bridge.respond(
            request_id, body.approved, accept_all=body.accept_all,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="no such pending approval")
        return {"ok": True}

    # ---- 事后评审（变更台账裁决）----

    @router.get("/review/pending")
    def pending_review() -> dict:
        review = host.review
        return {
            "pending_count": review.pending_count,
            "summary": review.pending_summary_text(),
            "records": [_record_dict(r) for r in review.ledger.pending_records()],
        }

    @router.post("/review/accept")
    def accept_changes(body: ReviewAction) -> dict:
        return {"result": host.review.accept(body.target)}

    @router.post("/review/revert")
    def revert_changes(body: ReviewAction) -> dict:
        return {"result": host.review.revert(body.target)}

    return router
