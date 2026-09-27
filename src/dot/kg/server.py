"""
dot.kg.server — KG RAG FastAPI 服务

独立的知识图谱 RAG 服务，不依赖原有的 dot.rag 模块。

路由:
  POST /ingest/text          索引文本
  POST /ingest/file          索引文件
  POST /ingest/documents    批量索引文档
  POST /query               查询（SSE 流式）
  GET  /stats               统计信息
  GET  /entities            实体列表
  GET  /entities/{name}     实体详情
  GET  /search              搜索实体
  DELETE /clear             清空图谱
  GET  /graph               导出图数据

启动: uvicorn dot.kg.server:app --reload --port 8001
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import KGSettings
from .pipeline import KGPipeline

__all__ = ["app", "main"]

app = FastAPI(title="dot.kg", version="1.0.0", description="Knowledge Graph RAG Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_settings = KGSettings()
_pipeline = KGPipeline(_settings)

# 加载已持久化的图数据
_pipeline._load_persisted()

# ============================================================
# 请求/响应模型
# ============================================================


class IngestTextRequest(BaseModel):
    text: str
    source: str = ""


class QueryRequest(BaseModel):
    question: str


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)


# ============================================================
# 索引路由
# ============================================================


@app.post("/ingest/text")
async def ingest_text(req: IngestTextRequest):
    """索引文本"""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")

    result = await _pipeline.aingest_text(req.text, req.source)
    return result.__dict__


@app.post("/ingest/file")
async def ingest_file(file: UploadFile):
    """索引上传的文件"""
    # 保存到临时目录
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".txt", ".md", ".pdf"}:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")

    temp_path = Path(".dot/kg/temp") / file.filename
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    with temp_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        result = _pipeline.ingest_file(temp_path)
        return result.__dict__
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.post("/ingest/documents")
async def ingest_documents(texts: list[str]):
    """批量索引文档"""
    if not texts:
        raise HTTPException(status_code=400, detail="texts is empty")

    result = _pipeline.ingest_documents(texts)
    return result.__dict__


# ============================================================
# 查询路由
# ============================================================


@app.post("/query")
async def query(req: QueryRequest):
    """查询（SSE 流式）"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")

    async def event_stream():
        async for event in _pipeline.astream(req.question):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/query/sync")
async def query_sync(req: QueryRequest):
    """同步查询（非流式）"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")

    result = await _pipeline.aquery(req.question)
    return result.__dict__


# ============================================================
# 管理路由
# ============================================================


@app.get("/stats")
async def stats():
    """统计信息"""
    return _pipeline.stats()


@app.delete("/clear")
async def clear():
    """清空图谱"""
    _pipeline.clear()
    return {"ok": True, "message": "Graph cleared"}


# ============================================================
# 实体路由
# ============================================================


@app.get("/entities")
async def list_entities(type: str | None = None, limit: int = 50):
    """实体列表"""
    if type:
        nodes = _pipeline.graph.get_nodes_by_type(type.upper())
    else:
        nodes = list(_pipeline.graph._nodes.values())[:limit]

    return {
        "entities": [n.to_dict() for n in nodes],
        "total": _pipeline.graph.node_count,
    }


@app.get("/entities/{name}")
async def get_entity(name: str):
    """实体详情"""
    entity = _pipeline.retriever.get_entity_info(name)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity not found: {name}")
    return entity


@app.get("/search")
async def search_entities(q: str, limit: int = 10):
    """搜索实体"""
    if not q.strip():
        return {"entities": [], "total": 0}

    entities = _pipeline.search_entities(q)
    return {
        "entities": entities[:limit],
        "total": len(entities),
    }


# ============================================================
# 图数据路由
# ============================================================


@app.get("/graph")
async def get_graph():
    """导出图数据"""
    return _pipeline.graph.to_dict()


@app.get("/paths")
async def find_paths(source: str, target: str, max_depth: int = 3):
    """查找两实体间的路径"""
    paths = _pipeline.retriever.find_paths(source, target, max_depth)
    return {"paths": paths}


# ============================================================
# 启动
# ============================================================


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")


if __name__ == "__main__":
    main()
