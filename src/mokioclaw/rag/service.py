"""FastAPI 应用：RAG Web 服务

路由：
- POST /ingest/file   上传文件
- POST /ingest/text   文本内容
- POST /ingest/url    URL 网页
- POST /preview/split 切片预览（不入库，可视化父子分块）
- POST /query         全链路 RAG（query改写→self-query→混合检索→RRF→rerank→
                       LIM重排→上下文构建→答案生成→引用溯源→guardrails→缓存）
- GET  /documents     列出已入库文档
- DELETE /documents/{doc_id}  删除文档（逻辑删除）
- GET  /trace/{trace_id}      按 trace_id 查单次链路
- POST /cache/clear   清空语义缓存
- GET  /health        健康检查

若 web/dist 存在，FastAPI 自动 mount 到 "/" 提供前端静态托管
（rag serve 启动后浏览器直接开 http://127.0.0.1:8000 即用）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_rag_dir, find_project_root
from mokioclaw.rag.loader import load_file, load_text, load_url
from mokioclaw.rag.security import sanitize_doc_id
from mokioclaw.rag.splitter import Chunk, StructureAwareSplitter
from mokioclaw.rag.store import ChromaStore

logger = get_logger(__name__)

# 上传限制
_MAX_UPLOAD_BYTES = int(os.getenv("RAG_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))  # 20MB
_ALLOWED_UPLOAD_SUFFIX = frozenset({".md", ".txt", ".pdf", ".markdown", ".rst", ".html", ".htm"})


# ===== 请求/响应模型（模块级，供 FastAPI 正确解析参数签名）=====
from pydantic import BaseModel, Field


class TextIngest(BaseModel):
    content: str
    source: str = "text"
    doc_id: str | None = None


class UrlIngest(BaseModel):
    url: str
    doc_id: str | None = None


class QueryIn(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=50)
    filter: dict[str, Any] | None = None
    # 高级 RAG 开关（默认关，opt-in）
    rewrite: bool = False        # query 改写（multi-query/hyde/step-back）
    self_query: bool = False      # self-query 结构化过滤解析
    generate_answer: bool = False  # 是否调 LLM 生成带引用答案
    use_cache: bool = True        # 是否用语义缓存


class SplitPreviewIn(BaseModel):
    content: str
    source: str = "preview"
    parent_size: int = Field(default=2000, ge=100, le=20000)
    child_size: int = Field(default=500, ge=50, le=10000)
    child_overlap: int = Field(default=80, ge=0, le=2000)


def create_app(
    store: ChromaStore | None = None,
    splitter: StructureAwareSplitter | None = None,
    retriever: Any | None = None,
    cache: Any | None = None,
) -> Any:
    """创建 FastAPI 应用

    Args:
        store: 已初始化的 ChromaStore（测试可注入）。None 时用全局单例。
        splitter: 分割器（测试可注入）。None 时用默认配置。
        retriever: 混合检索器（测试可注入）。None 时用 HybridRetriever。
        cache: 语义缓存（测试可注入）。None 时用 LocalFileCache。
    """
    from fastapi import FastAPI, Header, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import JSONResponse

    app = FastAPI(title="MokioClaw RAG", version="0.4.1")

    # CORS：开发时前端跑在 vite dev server (5173) 调后端 8000
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 可选 API Token：设置 RAG_API_TOKEN 后，除 /health 外需 Header: X-RAG-Token
    _api_token = os.getenv("RAG_API_TOKEN", "").strip()

    @app.middleware("http")
    async def _auth_middleware(request, call_next):  # type: ignore[no-untyped-def]
        import secrets

        if not _api_token:
            return await call_next(request)
        # CORS 预检必须豁免：浏览器不带自定义 Header 发 OPTIONS，
        # 401 会让带 token 的前端请求在预检阶段直接失败
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path or ""
        if path in {"/health", "/docs", "/openapi.json", "/redoc"} or path.startswith("/assets"):
            return await call_next(request)
        # 静态前端页面 + 根路径：仅放行明确安全的 GET 路径（正向允许列表）
        if request.method == "GET" and (path in {"/"} or path.startswith("/assets")):
            return await call_next(request)
        token = request.headers.get("X-RAG-Token") or request.headers.get("x-rag-token") or ""
        # compare_digest 防时序侧信道
        if not secrets.compare_digest(token, _api_token):
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
        return await call_next(request)

    _store = store
    _splitter = splitter
    _retriever = retriever
    _cache = cache

    def _get_store() -> ChromaStore:
        # 外层变量： nonlocal
        nonlocal _store
        if _store is None:
            from mokioclaw.rag.embedding import create_embedder
            _store = ChromaStore(embedder=create_embedder())
        return _store

    def _get_splitter() -> StructureAwareSplitter:
        nonlocal _splitter
        if _splitter is None:
            chunk_size = _env_int("RAG_CHUNK_SIZE", 1000)
            chunk_overlap = _env_int("RAG_CHUNK_OVERLAP", 200)
            _splitter = StructureAwareSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return _splitter

    def _get_retriever() -> Any:
        nonlocal _retriever
        if _retriever is None:
            from mokioclaw.rag.retrieval import HybridRetriever
            from mokioclaw.rag.reranker import Reranker
            s = _get_store()
            r = HybridRetriever(
                backend=s.backend,
                embedder=s.embedder,
                top_k=5,
            )
            r.reranker = Reranker()
            _retriever = r
        return _retriever

    def _get_cache() -> Any:
        nonlocal _cache
        if _cache is None:
            from mokioclaw.rag.cache import LocalFileCache
            s = _get_store()
            _cache = LocalFileCache(embedder=s.embedder)
        return _cache

    def _ingest_pages(pages: list[tuple[str, int | None]], source: str, doc_id: str) -> int:
        """把解析出的页面 chunks 入库，返回 chunk 总数"""
        splitter = _get_splitter()
        all_chunks: list[Chunk] = []
        for text, page in pages:
            chunks = splitter.split_text(text, source=source, doc_id=doc_id, page=page)
            all_chunks.extend(chunks)
        if all_chunks:
            _get_store().add(all_chunks)
        return len(all_chunks)

    # ===== 路由 =====
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "persist_dir": str(_get_store().persist_dir)}

    @app.post("/ingest/text")
    def ingest_text(body: TextIngest) -> dict[str, Any]:
        # hash() 受 PYTHONHASHSEED 随机化，重启后同一文本生成不同 doc_id → 重复摄取
        import hashlib

        digest = hashlib.sha256(body.content.encode("utf-8")).hexdigest()[:8]
        raw_id = body.doc_id or f"text:{digest}"
        doc_id = sanitize_doc_id(raw_id)
        pages = load_text(body.content)
        count = _ingest_pages(pages, source=body.source, doc_id=doc_id)
        return {"ok": True, "doc_id": doc_id, "chunks": count}

    @app.post("/ingest/url")
    def ingest_url(body: UrlIngest) -> dict[str, Any]:
        raw_id = body.doc_id or f"url:{body.url}"
        doc_id = sanitize_doc_id(raw_id)
        pages = load_url(body.url)
        if not pages:
            return {"ok": False, "error": "failed to fetch url (blocked or unreachable)", "doc_id": doc_id, "chunks": 0}
        count = _ingest_pages(pages, source=body.url, doc_id=doc_id)
        return {"ok": True, "doc_id": doc_id, "chunks": count}

    @app.post("/ingest/file")
    async def ingest_file(file: UploadFile) -> dict[str, Any]:
        import tempfile
        filename = file.filename or "upload"
        suffix = os.path.splitext(filename)[1].lower()
        if suffix and suffix not in _ALLOWED_UPLOAD_SUFFIX:
            return {
                "ok": False,
                "error": f"unsupported file type: {suffix}",
                "doc_id": "",
                "chunks": 0,
            }
        doc_id = sanitize_doc_id(f"file:{filename}")
        # 限流读入，防止超大上传
        chunks_buf: list[bytes] = []
        total = 0
        while True:
            piece = await file.read(1024 * 1024)
            if not piece:
                break
            total += len(piece)
            if total > _MAX_UPLOAD_BYTES:
                return {
                    "ok": False,
                    "error": f"file too large (max {_MAX_UPLOAD_BYTES} bytes)",
                    "doc_id": doc_id,
                    "chunks": 0,
                }
            chunks_buf.append(piece)
        data = b"".join(chunks_buf)
        with tempfile.NamedTemporaryFile(suffix=suffix or ".txt", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            pages = load_file(Path(tmp_path))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if not pages:
            return {"ok": False, "error": "empty or unsupported file", "doc_id": doc_id, "chunks": 0}
        count = _ingest_pages(pages, source=filename, doc_id=doc_id)
        return {"ok": True, "doc_id": doc_id, "chunks": count, "source": filename}

    @app.post("/query")
    def query(body: QueryIn) -> dict[str, Any]:
        """全链路 RAG query"""
        from mokioclaw.rag.trace import new_trace

        trace = new_trace(body.query)
        try:
            retriever = _get_retriever()
            store = _get_store()

            # 0. 语义缓存查（generate_answer 时才有答案可缓存）
            # filter 非空时禁用缓存，避免串答案；cache_key 纳入 k/开关
            from mokioclaw.rag.cache import make_cache_key

            cache_key = make_cache_key(
                body.query,
                k=body.k,
                filter=body.filter,
                rewrite=body.rewrite,
                self_query=body.self_query,
                generate_answer=body.generate_answer,
            )
            allow_cache = body.use_cache and body.generate_answer and not body.filter
            if allow_cache:
                try:
                    cache = _get_cache()
                    q_emb = store.embedder.embed_query(body.query)
                    hit = cache.get(body.query, q_emb, cache_key=cache_key)
                    if hit is not None:
                        trace.record("cache_hit")
                        trace.save()
                        return {
                            "ok": True,
                            "query": body.query,
                            "answer": hit.answer,
                            "trace_id": trace.trace_id,
                            "cached": True,
                            "citations": hit.citations,
                            "chunks": [],
                        }
                except Exception as exc:  # noqa: BLE001
                    trace.mark_degraded("cache_read_failed")

            # 1. query 改写（可选）
            queries = [body.query]
            if body.rewrite:
                from mokioclaw.rag.query_transform import transform_query
                queries = transform_query(
                    body.query, multi_query=True, hyde=True, step_back=False,
                )
                trace.record("query_rewrite", queries=queries)

            # 2. self-query 过滤解析（可选）
            where = body.filter
            if body.self_query:
                from mokioclaw.rag.self_query import parse_filter
                try:
                    sq_filter = parse_filter(body.query)
                    if sq_filter:
                        where = {**(where or {}), **sq_filter}
                        trace.record("self_query", filter=sq_filter)
                except Exception as exc:  # noqa: BLE001
                    trace.mark_degraded("self_query_failed")

            # 3. 多路检索（query 改写时对每个 query 检索后合并）
            from mokioclaw.rag.types import ParentChunk
            all_parents: list[ParentChunk] = []
            seen_pids: set[str] = set()
            for q in queries:
                parents = retriever.retrieve(q, where=where, k=body.k)
                for p in parents:
                    if p.parent_id not in seen_pids:
                        seen_pids.add(p.parent_id)
                        all_parents.append(p)
            trace.record("retrieve", hits=len(all_parents), queries=len(queries))

            # 4. rerank（若可用）
            reranker = getattr(retriever, "reranker", None)
            if reranker is not None and reranker.available and all_parents:
                all_parents = reranker.rerank(body.query, all_parents, top_n=body.k)
                stub = bool(getattr(reranker, "_stub_mode", False))
                trace.record("rerank", hits=len(all_parents), stub=stub)
                if stub:
                    trace.mark_degraded("reranker_lexical_stub")
            elif reranker is not None:
                trace.mark_degraded("reranker_unavailable")

            # 5. Lost-in-the-Middle 重排
            from mokioclaw.rag.reorder import reorder_lost_in_the_middle
            all_parents = reorder_lost_in_the_middle(all_parents)
            trace.record("lim_reorder")

            # 6. 生成答案（可选）
            answer_text = ""
            citations: list[dict[str, Any]] = []
            if body.generate_answer and all_parents:
                from mokioclaw.rag.answer import generate_answer
                from mokioclaw.rag.citation import build_citation_refs
                from mokioclaw.rag.guardrails import apply_guardrails

                answer_text, ctx_citations = generate_answer(
                    body.query, all_parents,
                )
                # 7. 引用溯源
                refs = build_citation_refs(answer_text, ctx_citations)
                citations = [
                    {"index": r.index, "doc_id": r.doc_id, "source": r.source}
                    for r in refs
                ]
                trace.record("citation", refs=len(citations))

                # 8. guardrails 输出护栏
                answer_text, guard = apply_guardrails(answer_text, has_citations=bool(citations))
                if not guard.passed:
                    trace.mark_degraded(f"guardrail:{guard.reason}")

                # 9. 写缓存
                if allow_cache:
                    try:
                        from mokioclaw.rag.cache import CacheEntry
                        import time
                        cache = _get_cache()
                        cache.put(CacheEntry(
                            query=body.query,
                            answer=answer_text,
                            embedding=store.embedder.embed_query(body.query),
                            created_at=time.time(),
                            ttl=3600,
                            citations=citations,
                            cache_key=cache_key,
                        ))
                        trace.record("cache_put")
                    except Exception as exc:  # noqa: BLE001
                        trace.mark_degraded("cache_write_failed")

            trace.save()
            return {
                "ok": True,
                "query": body.query,
                "answer": answer_text,
                "trace_id": trace.trace_id,
                "degraded": trace.degraded,
                "citations": citations,
                "chunks": [
                    {"content": p.content, "metadata": p.metadata}
                    for p in all_parents
                ],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("rag query failed [trace=%s]: %s", trace.trace_id, exc)
            trace.mark_degraded(f"query_error: {type(exc).__name__}")
            trace.save()
            return {
                "ok": False,
                "error": str(exc),
                "trace_id": trace.trace_id,
                "chunks": [],
            }

    @app.get("/documents")
    def list_docs() -> dict[str, Any]:
        return {"ok": True, "documents": _get_store().list_docs()}

    @app.delete("/documents/{doc_id}")
    def delete_doc(doc_id: str) -> dict[str, Any]:
        safe_id = sanitize_doc_id(doc_id)
        _get_store().delete_doc(safe_id)
        return {"ok": True, "doc_id": safe_id}

    @app.post("/cache/clear")
    def clear_cache() -> dict[str, Any]:
        try:
            n = _get_cache().clear()
            return {"ok": True, "cleared": n}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ===== 切片预览（不入库，可视化父子分块）=====
    @app.post("/preview/split")
    def preview_split(body: SplitPreviewIn) -> dict[str, Any]:
        """切片预览：调 splitter.split_text_parent_child，不入库"""
        splitter = StructureAwareSplitter(
            chunk_size=body.child_size, chunk_overlap=body.child_overlap,
        )
        try:
            parents, children = splitter.split_text_parent_child(
                body.content,
                source=body.source,
                doc_id="preview",
                parent_size=body.parent_size,
                child_size=body.child_size,
                child_overlap=body.child_overlap,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        avg_child_len = (
            sum(len(c.content) for c in children) / len(children) if children else 0
        )
        return {
            "ok": True,
            "parents": [
                {"content": p.content, "metadata": p.metadata} for p in parents
            ],
            "children": [
                {"content": c.content, "metadata": c.metadata} for c in children
            ],
            "stats": {
                "parent_count": len(parents),
                "child_count": len(children),
                "avg_child_len": round(avg_child_len, 1),
            },
        }

    # ===== 按 trace_id 查单次链路 =====
    @app.get("/trace/{trace_id}")
    def get_trace(trace_id: str) -> Any:
        """读 .mokioclaw/rag/traces/{trace_id}.jsonl，返回最后一条记录"""
        # 安全校验：trace_id 只允许字母数字连字符
        safe = "".join(c for c in trace_id if c.isalnum() or c in "-_")
        if safe != trace_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid trace_id"})
        trace_file = default_rag_dir() / "traces" / f"{safe}.jsonl"
        if not trace_file.exists():
            return JSONResponse(status_code=404, content={"ok": False, "error": "trace not found"})
        try:
            last: dict[str, Any] | None = None
            with trace_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        last = rec
            if last is None:
                return JSONResponse(status_code=404, content={"ok": False, "error": "trace empty"})
            return {"ok": True, "trace": last}
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    # ===== 静态文件托管（前端 web/dist）=====
    web_dist = find_project_root() / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")

    return app


def _env_int(name: str, default: int) -> int:
    """读环境变量为 int（复用 openai_provider 的同名模式）"""
    raw = os.getenv(name, str(default))
    try:
        val = int(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default
