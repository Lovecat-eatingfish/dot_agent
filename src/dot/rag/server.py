"""
dot.rag.server — FastAPI Web 服务

路由（vite dev 代理会把 /api 前缀剥掉，所以这里不带前缀）：
  POST /query                     SSE 流式问答（stage/delta/done/error 事件）
  POST /search                    只检索，返回召回片段
  GET  /documents                 文档列表（版本/状态）
  POST /documents/upload          上传文件入库（多文件）
  POST /documents/text            粘贴文本入库
  POST /documents/ingest-dir      目录批量入库
  DELETE /documents/{doc_id}      逻辑删除（同时清向量）
  POST /split/preview             父子分块预览（不入库）
  GET  /traces                    trace 列表
  GET  /traces/{trace_id}         单次查询完整链路

生产模式自动托管 web/dist/（同源，无需代理）。
启动：uv run python -m dot.rag serve  或  uv run python -m dot.rag.server
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import RagSettings
from .loader import split_parent_child
from .pipeline import RAGPipeline

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DIST_DIR = PROJECT_ROOT / "web" / "dist"

app = FastAPI(title="dot.rag Web", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地开发工具，放开跨域
    allow_methods=["*"],
    allow_headers=["*"],
)

_settings = RagSettings()
_pipeline = RAGPipeline(_settings)

_TEXT_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}


# ---------- 请求/响应模型 ----------


class QueryRequest(BaseModel):
    question: str
    top_k: int | None = Field(default=None, ge=1, le=20)


class ChatRequest(QueryRequest):
    session_id: str | None = None  # 不传则自动新建会话


class SessionCreateRequest(BaseModel):
    title: str = ""


class SessionRenameRequest(BaseModel):
    title: str


class SearchRequest(QueryRequest):
    pass


class TextIngestRequest(BaseModel):
    text: str
    filename: str = "pasted_text.md"


class DirIngestRequest(BaseModel):
    dir: str | None = None


class SplitPreviewRequest(BaseModel):
    text: str
    parent_size: int = Field(default=1200, ge=100, le=5000)
    parent_overlap: int = Field(default=100, ge=0, le=1000)
    child_size: int = Field(default=500, ge=50, le=2000)
    child_overlap: int = Field(default=50, ge=0, le=500)


# ---------- 查询 ----------

# 业务路由统一挂在 api 上，最后以 根路径 + /api 前缀 挂载两次：
#   - 生产（FastAPI 直接托管 dist）：前端请求 /api/xxx → 命中 /api 前缀
#   - 开发（vite 代理剥掉 /api）：    转发到 8000 的 /xxx → 命中根路径
api = APIRouter()


@api.post("/query")
async def query(req: QueryRequest):
    """SSE 流式问答"""

    async def event_stream():
        async for event in _pipeline.astream(req.question, req.top_k):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.post("/search")
async def search(req: SearchRequest):
    """只检索，返回召回片段（调试检索质量）"""
    docs = _pipeline.retrieve(req.question, req.top_k)
    return {
        "chunks": [
            {
                "source": d.metadata.get("source", ""),
                "score": d.metadata.get("score"),
                "text": d.metadata.get("child_text", d.page_content),
                "parent_text": d.page_content,
            }
            for d in docs
        ]
    }


# ---------- 会话聊天 ----------


@api.post("/chat")
async def chat(req: ChatRequest):
    """会话式 SSE 流式问答（多轮，历史上下文自动带入）

    session_id 不传时自动新建会话（新建时事件流首条 session 事件会带回 id）。
    """
    session_id = req.session_id
    auto_created = False
    if not session_id:
        session_id = _pipeline.create_session()["id"]
        auto_created = True

    async def event_stream():
        if auto_created:
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'title': ''}, ensure_ascii=False)}\n\n"
        try:
            async for event in _pipeline.achat_stream(session_id, req.question, req.top_k):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # 会话不存在等
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/sessions")
async def list_sessions():
    return {"sessions": _pipeline.list_sessions()}


@api.post("/sessions")
async def create_session(req: SessionCreateRequest):
    return _pipeline.create_session(req.title)


@api.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if not _pipeline.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return {"ok": True}


@api.get("/sessions/{session_id}/messages")
async def list_session_messages(session_id: str):
    if not _pipeline.store.get_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return {"messages": _pipeline.list_messages(session_id)}


@api.patch("/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest):
    if not _pipeline.store.update_session_title(session_id, req.title):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return {"ok": True}


# ---------- 文档管理 ----------


@api.get("/documents")
async def list_documents():
    return {"documents": _pipeline.list_documents()}


@api.post("/documents/upload")
async def upload_documents(files: list[UploadFile]):
    """上传文件入库：保存到 docs 目录 → 父子分块 → 向量化 → 登记"""
    docs_dir = Path(_settings.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)

    results, errors = [], []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in _TEXT_SUFFIXES:
            errors.append({"filename": file.filename, "error": f"不支持的格式 {suffix}，支持 {sorted(_TEXT_SUFFIXES)}"})
            continue
        save_path = docs_dir / Path(file.filename).name
        with save_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            results.append(_pipeline.ingest_file(save_path))
        except Exception as exc:
            errors.append({"filename": file.filename, "error": str(exc)})
    return {"ingested": results, "errors": errors}


@api.post("/documents/text")
async def ingest_text(req: TextIngestRequest):
    """粘贴文本入库"""
    try:
        result = _pipeline.ingest_text(req.text, req.filename)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/documents/ingest-dir")
async def ingest_dir(req: DirIngestRequest):
    """目录批量入库"""
    return {"ingested": _pipeline.ingest_dir(req.dir)}


@api.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    if not _pipeline.delete_document(doc_id):
        raise HTTPException(status_code=404, detail=f"文档不存在: {doc_id}")
    return {"ok": True}


@api.get("/documents/{doc_id}/chunks")
async def get_document_chunks(doc_id: str):
    """某文档实际入库的切片结果（父块 → 子块树）"""
    if not _pipeline.store.get_document(doc_id):
        raise HTTPException(status_code=404, detail=f"文档不存在: {doc_id}")
    return _pipeline.get_document_chunks(doc_id)


# ---------- 切片预览 ----------


@api.post("/split/preview")
async def split_preview(req: SplitPreviewRequest):
    """父子分块预览（不入库），返回 parent → children 树"""
    families = split_parent_child(
        req.text,
        parent_size=req.parent_size,
        parent_overlap=req.parent_overlap,
        child_size=req.child_size,
        child_overlap=req.child_overlap,
    )
    return {
        "families": families,
        "stats": {
            "parents": len(families),
            "children": sum(len(f["children"]) for f in families),
            "chars": len(req.text),
        },
    }


# ---------- trace ----------


@api.get("/traces")
async def list_traces(limit: int = 50):
    return {"traces": _pipeline.list_traces(limit)}


@api.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    trace = _pipeline.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"trace 不存在: {trace_id}")
    return trace


# ---------- 挂载：根路径 + /api 前缀（开发代理与生产同源都能命中） ----------

app.include_router(api)
app.include_router(api, prefix="/api")

# ---------- 静态托管（生产模式） ----------


class SPAStaticFiles(StaticFiles):
    """SPA 路由回退：/trace 等前端路由在磁盘上没有同名文件，回退到 index.html

    Starlette 的 StaticFiles 找不到文件时抛 HTTPException(404)（旧版本返回响应），
    两种情况都兜住。
    """

    async def get_response(self, path: str, scope):
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
        if resp.status_code == 404:
            return await super().get_response("index.html", scope)
        return resp


if DIST_DIR.is_dir():
    app.mount("/", SPAStaticFiles(directory=str(DIST_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=_settings.server_host, port=_settings.server_port, log_level="info")


if __name__ == "__main__":
    main()
