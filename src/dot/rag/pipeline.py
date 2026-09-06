"""
dot.rag.pipeline — RAG 全链路编排

串起标准链路：
  索引: 上传文件/粘贴文本/本地目录 → 父子分块 → 向量化入库 → 登记文档（版本管理）
  查询: 检索子块 → 回带父块上下文 → LLM 流式生成 → 记录 trace 链路
  管理: 文档列表 / 逻辑删除（同时清向量）
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.documents import Document

from dot.ai.events import TextDeltaEvent
from dot.ai.types import AssistantMessage, TextContent, UserMessage

from .config import RagSettings
from .generator import generate_answer
from .loader import split_documents, split_parent_child_documents
from .retrieval import HybridRetriever
from .store import DocumentRecord, MessageRecord, MetadataStoreBase, TraceRecord, TraceStep, create_metadata_store
from .vectorstore import delete_by_doc, get_vectorstore


@dataclass
class RagResult:
    """一次 RAG 问答的完整结果"""

    question: str
    answer: str = ""
    source_documents: list[Document] = field(default_factory=list)
    trace_id: str = ""


class RAGPipeline:
    """RAG Pipeline：索引 + 检索 + 生成 + 文档管理"""

    def __init__(self, settings: RagSettings | None = None):
        self.settings = settings or RagSettings()
        self.store = create_metadata_store(self.settings)
        self.retriever = HybridRetriever(self.settings)

    # ---------- 索引阶段 ----------

    def _index_chunks(self, chunks: list[Document]) -> None:
        """写入向量库（带 uuid 主键，避免重复）"""
        if not chunks:
            return
        vs = get_vectorstore(settings=self.settings)
        vs.add_documents(chunks, ids=[uuid4().hex for _ in chunks])
        self.retriever.invalidate()  # BM25 语料变更

    def ingest_file(self, path: str | Path) -> dict:
        """入库单个文件：同名文档视为更新，版本 +1 并清除旧向量"""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        from .loader import load_file

        text = load_file(path)
        if not text.strip():
            raise ValueError(f"未能从 {path.name} 提取到文本")

        existing = self.store.get_active_by_filename(path.name)
        doc_id = existing.id if existing else self.store.new_doc_id()
        if existing:
            delete_by_doc(existing.id, self.settings)

        chunks = self._make_chunks(text, source=path.name, doc_id=doc_id)
        self._index_chunks(chunks)
        record = self.store.upsert_document(DocumentRecord(
            id=doc_id,
            filename=path.name,
            path=str(path),
            chunk_count=len(chunks),
        ))
        return {"id": record.id, "filename": record.filename, "version": record.version, "chunk_count": record.chunk_count}

    def ingest_text(self, text: str, filename: str) -> dict:
        """入库粘贴的文本"""
        if not text.strip():
            raise ValueError("文本为空")
        existing = self.store.get_active_by_filename(filename)
        doc_id = existing.id if existing else self.store.new_doc_id()
        if existing:
            delete_by_doc(existing.id, self.settings)

        chunks = self._make_chunks(text, source=filename, doc_id=doc_id)
        self._index_chunks(chunks)
        record = self.store.upsert_document(DocumentRecord(
            id=doc_id,
            filename=filename,
            path="",
            chunk_count=len(chunks),
        ))
        return {"id": record.id, "filename": record.filename, "version": record.version, "chunk_count": record.chunk_count}

    def ingest_dir(self, docs_dir: str | Path | None = None) -> list[dict]:
        """入库目录下所有支持格式的文件（逐个登记，方便版本管理）"""
        docs_dir = Path(docs_dir or self.settings.docs_dir)
        results = []
        for path in sorted(docs_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".txt", ".md", ".pdf", ".docx"}:
                try:
                    results.append(self.ingest_file(path))
                except ValueError:
                    continue
        return results

    def _make_chunks(self, text: str, source: str, doc_id: str) -> list[Document]:
        if self.settings.enable_parent_child:
            chunks = split_parent_child_documents(
                [Document(page_content=text, metadata={"source": source})], self.settings
            )
        else:
            chunks = split_documents(
                [Document(page_content=text, metadata={"source": source})], self.settings
            )
        for c in chunks:  # doc_id 写进向量元数据，逻辑删除时按它清除向量
            c.metadata["doc_id"] = doc_id
        return chunks

    # ---------- 文档管理 ----------

    def list_documents(self) -> list[dict]:
        return [d.__dict__ for d in self.store.list_documents()]

    def delete_document(self, doc_id: str) -> bool:
        """逻辑删除：登记表打标记，同时清掉向量"""
        doc = self.store.get_document(doc_id)
        if not doc:
            return False
        delete_by_doc(doc_id, self.settings)
        self.retriever.invalidate()  # BM25 语料变更
        return self.store.soft_delete_document(doc_id)

    def get_document_chunks(self, doc_id: str) -> dict:
        """某文档实际入库的切片结果，按父块 → 子块组织（前端切片查看用）"""
        from .vectorstore import get_document_chunks

        chunks = get_document_chunks(doc_id, self.settings)

        families: list[dict] = []
        for c in chunks:
            if not families or families[-1]["parent_id"] != c["parent_id"]:
                families.append({
                    "parent_id": c["parent_id"] or f"c{c['parent_index']}",
                    "parent_text": c["parent_text"],
                    "children": [],
                })
            families[-1]["children"].append(c["text"])

        return {
            "families": families,
            "stats": {
                "parents": len(families),
                "children": len(chunks),
                "chars": sum(len(f["parent_text"]) for f in families),
            },
        }

    # ---------- 查询阶段 ----------

    def _assemble_hits(self, hits: list) -> list[Document]:
        """子块命中 → 回带父块文本作为上下文（页面同时展示子块原文）"""
        results: list[Document] = []
        for doc, score in hits:
            meta = dict(doc.metadata)
            child_text = doc.page_content
            parent_text = meta.pop("parent_text", None)
            results.append(Document(
                page_content=parent_text or child_text,
                metadata={
                    **meta,
                    "child_text": child_text,
                    "score": round(float(score), 4),
                },
            ))
        return results

    def retrieve(self, question: str, top_k: int | None = None) -> list[Document]:
        """检索（不带 trace 计时的便捷入口），走完整检索管道"""
        top_k = top_k or self.settings.top_k
        hits, _ = self.retriever.retrieve(question, top_k)
        return self._assemble_hits(hits)

    def retrieve_with_trace(self, question: str, top_k: int | None = None):
        """带 span 计时的检索：返回 (结果片段, retrieve span)

        span 树：retrieve → vector_search / bm25_search / fusion / rerank /
                compress / parent_assemble（子 span 由检索管道产出）
        """
        top_k = top_k or self.settings.top_k
        t0 = time.perf_counter()
        span = TraceStep(stage="retrieve", start_ms=0.0)

        hits, children = self.retriever.retrieve(question, top_k)
        span.children.extend(children)

        t = time.perf_counter()
        results = self._assemble_hits(hits)
        span.add_child(TraceStep(
            stage="parent_assemble", start_ms=(time.perf_counter() - t0) * 1000,
            cost_ms=(time.perf_counter() - t) * 1000,
        ))

        span.cost_ms = (time.perf_counter() - t0) * 1000
        return results, span

    async def astream(self, question: str, top_k: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """流式问答（无会话），事件流见 _astream_core"""
        async for event in self._astream_core(question, top_k):
            yield event

    async def _astream_core(
        self,
        question: str,
        top_k: int | None = None,
        history: list | None = None,
        session_id: str | None = None,
        retrieval_question: str | None = None,
        rewrite_span: TraceStep | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """检索 + 生成的核心事件流，产出统一事件（dict）：

        {"type": "stage",  "stage": "retrieve", "cost_ms": ..., "chunks": [...]}
        {"type": "delta",  "text": "..."}
        {"type": "done",   "trace_id": ..., "answer": ..., "sources": [...], "message_id": ...}
        {"type": "error",  "message": ...}

        history：多轮会话历史（User/Assistant 消息），只影响 LLM 输入；
        session_id：提供时把 assistant 回答落库到该会话；
        retrieval_question / rewrite_span：会话改写后的检索问句及其 span。
        trace 记录为 span 树，retrieve 下含检索管道各阶段子 span。
        """
        trace_id = self.store.new_trace_id()
        chunks: list[Document] = []
        answer_parts: list[str] = []
        status = "done"
        root = TraceStep(stage="query", start_ms=0.0)

        t0 = time.perf_counter()
        try:
            chunks, retrieve_span = self.retrieve_with_trace(retrieval_question or question, top_k)
            if rewrite_span is not None:
                retrieve_span.children.insert(0, rewrite_span)
                retrieve_span.cost_ms += rewrite_span.cost_ms
            root.add_child(retrieve_span)
            yield {
                "type": "stage", "stage": "retrieve", "cost_ms": round(retrieve_span.cost_ms, 1),
                "chunks": [{"source": c.metadata.get("source", ""), "score": c.metadata.get("score"),
                            "text": c.metadata.get("child_text", c.page_content),
                            "parent_text": c.page_content} for c in chunks],
            }

            generate_span = TraceStep(
                stage="generate",
                start_ms=(time.perf_counter() - t0) * 1000,
                detail={"model": self.settings.resolve_generate_model()},
            )
            root.add_child(generate_span)

            from .generator import build_prompt

            t = time.perf_counter()
            system = build_prompt(question, chunks, self.settings)
            generate_span.add_child(TraceStep(
                stage="build_prompt", start_ms=(time.perf_counter() - t0) * 1000,
                cost_ms=(time.perf_counter() - t) * 1000,
                detail={"context_chars": len(system), "docs": len(chunks)},
            ))

            t = time.perf_counter()
            first_token_ms = 0.0
            deltas = 0
            async for event in generate_answer(question, chunks, self.settings, system=system, history=history):
                if isinstance(event, TextDeltaEvent):
                    if first_token_ms == 0.0:
                        first_token_ms = (time.perf_counter() - t) * 1000
                    deltas += 1
                    answer_parts.append(event.delta)
                    yield {"type": "delta", "text": event.delta}
            generate_span.add_child(TraceStep(
                stage="llm_stream", start_ms=(t - t0) * 1000, cost_ms=(time.perf_counter() - t) * 1000,
                detail={"first_token_ms": round(first_token_ms, 1), "deltas": deltas},
            ))
            generate_span.cost_ms = (time.perf_counter() - t0) * 1000 - retrieve_span.cost_ms

            root.cost_ms = (time.perf_counter() - t0) * 1000
            answer = "".join(answer_parts)
            sources = sorted({c.metadata.get("source", "") for c in chunks})

            message_id = ""
            if session_id:
                record = self.store.append_message(MessageRecord(
                    id=self.store.new_message_id(), session_id=session_id,
                    role="assistant", content=answer, trace_id=trace_id, sources=sources,
                ))
                message_id = record.id

            yield {
                "type": "done", "trace_id": trace_id, "answer": answer, "sources": sources,
                "message_id": message_id,
            }
        except Exception as exc:
            status = "error"
            root.cost_ms = (time.perf_counter() - t0) * 1000
            yield {"type": "error", "message": str(exc)}
        finally:
            self.store.insert_trace(TraceRecord(
                trace_id=trace_id, question=question, answer="".join(answer_parts),
                steps=[root], top_k=top_k or self.settings.top_k, chunk_count=len(chunks), status=status,
            ))

    async def aquery(self, question: str, top_k: int | None = None) -> RagResult:
        """非流式问答（内部复用 astream 的事件流，保证 trace 行为一致）"""
        result = RagResult(question=question)
        async for event in self.astream(question, top_k):
            if event["type"] == "stage":
                result.source_documents = [
                    Document(page_content=c["parent_text"], metadata={"source": c["source"], "score": c["score"]})
                    for c in event["chunks"]
                ]
            elif event["type"] == "done":
                result.answer = event["answer"]
                result.trace_id = event["trace_id"]
            elif event["type"] == "error":
                result.answer = f"[错误] {event['message']}"
        return result

    # ---------- 会话式多轮问答 ----------

    def create_session(self, title: str = "") -> dict:
        record = self.store.create_session(title)
        return record.__dict__

    def list_sessions(self) -> list[dict]:
        return [s.__dict__ for s in self.store.list_sessions()]

    def delete_session(self, session_id: str) -> bool:
        return self.store.delete_session(session_id)

    def list_messages(self, session_id: str) -> list[dict]:
        return [self._message_to_dict(m) for m in self.store.list_messages(session_id)]

    @staticmethod
    def _message_to_dict(m) -> dict:
        d = m.__dict__.copy()
        return d

    async def achat_stream(self, session_id: str, question: str, top_k: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """会话式流式问答：多轮历史 + 检索 + 生成，消息全部落库

        事件流在 astream 基础上增加：
        {"type": "session", "session_id": ..., "title": ...}   # 首条，前端同步会话标题
        done 事件带 message_id（assistant 消息 id）
        """
        session = self.store.get_session(session_id)
        if not session:
            yield {"type": "error", "message": f"会话不存在: {session_id}"}
            return

        # 用户消息落库；首个提问作为会话标题
        self.store.append_message(MessageRecord(
            id=self.store.new_message_id(), session_id=session_id, role="user", content=question,
        ))
        title = session.title
        if not title:
            title = question[:20]
            self.store.update_session_title(session_id, title)
        yield {"type": "session", "session_id": session_id, "title": title}

        # 多轮历史（不含刚落库的当前问题）：最近 6 条 = 3 轮
        history: list = []
        for m in self.store.list_messages(session_id)[:-1][-6:]:
            if m.role == "user":
                history.append(UserMessage(content=m.content))
            else:
                history.append(AssistantMessage(content=[TextContent(text=m.content)]))

        # 会话感知改写：把追问改写为独立问句，只影响检索，不影响呈现给 LLM 的问题
        retrieval_question, rewrite_span = question, None
        if history and self.settings.query_rewrite:
            import time as _time

            from .query_processor import rewrite_query

            t = _time.perf_counter()
            rewritten = await rewrite_query(question, history, self.settings)
            if rewritten and rewritten != question:
                retrieval_question = rewritten
                rewrite_span = TraceStep(
                    stage="rewrite", start_ms=0.0,
                    cost_ms=(_time.perf_counter() - t) * 1000,
                    detail={"rewritten": rewritten},
                )

        # 复用 astream 的检索/生成/span 记录，把历史插进 LLM 消息，回答落库到会话
        async for event in self._astream_core(
            question, top_k, history=history, session_id=session_id,
            retrieval_question=retrieval_question, rewrite_span=rewrite_span,
        ):
            yield event

    # ---------- trace ----------

    def list_traces(self, limit: int = 50) -> list[dict]:
        return self.store.list_traces(limit)

    def get_trace(self, trace_id: str) -> dict | None:
        return self.store.get_trace(trace_id)
