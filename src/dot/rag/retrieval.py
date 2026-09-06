"""
dot.rag.retrieval — 检索管道（M1）

把"检索"从单次向量调用重构为可组合管道：

    查询 → [向量召回] ┐
         → [BM25 召回] ┴→ RRF 融合 → 重排序 → 压缩过滤 → 子块命中列表

每个阶段都产出 TraceStep（由 pipeline 挂到 retrieve span 下，瀑布图自动展示）。
所有开关默认值见 config；关闭全部开关时退化为纯向量检索（原行为）。

BM25 语料来自 Chroma 中当前全部子块，入库/删除后由 pipeline 调 invalidate() 重建。
"""
from __future__ import annotations

import time
from typing import Any

from dot.ai.events import TextDeltaEvent
from dot.ai.providers.openai import OpenAIProvider
from langchain_core.documents import Document

from .config import RagSettings
from .store import TraceStep
from .vectorstore import get_vectorstore

# ---------- 轻量缓存：CrossEncoder 模型 & BM25 索引 ----------

_RERANKER_CACHE: dict[str, Any] = {}


def _get_reranker(model_name: str):
    if model_name not in _RERANKER_CACHE:
        import os

        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from sentence_transformers import CrossEncoder

        # max_length=256：子块都在 500 字内，缩短序列可显著降低 CPU 推理延迟
        _RERANKER_CACHE[model_name] = CrossEncoder(model_name, max_length=256)
    return _RERANKER_CACHE[model_name]


def _tokenize(text: str) -> list[str]:
    import jieba

    return [t for t in jieba.lcut(text.lower()) if t.strip()]


class BM25Index:
    """内存 BM25 索引（中文 jieba 分词），覆盖当前全部子块"""

    def __init__(self, chunks: list[dict]):
        from rank_bm25 import BM25Okapi

        self.chunks = chunks  # [{"key", "doc_id", "text", "metadata"}]
        self._bm25 = BM25Okapi([_tokenize(c["text"]) for c in chunks]) if chunks else None

    def search(self, query: str, k: int) -> list[tuple[dict, float]]:
        if not self._bm25:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(self.chunks, scores), key=lambda x: x[1], reverse=True)
        return [(c, float(s)) for c, s in ranked[:k] if s > 0]


def _chunk_key(doc_id: str, meta: dict, text: str) -> str:
    """去重键：同一条子块在两路召回中只保留一份"""
    if meta.get("parent_id"):
        return f"{doc_id}:{meta['parent_id']}:{meta.get('chunk_index', 0)}"
    return f"{doc_id}:hash:{hash(text) & 0xFFFFFF}"


class HybridRetriever:
    """检索管道。由 pipeline 持有（单例），入库/删除后调用 invalidate()"""

    def __init__(self, settings: RagSettings | None = None):
        self.settings = settings or RagSettings()
        self._bm25: BM25Index | None = None

    def invalidate(self) -> None:
        self._bm25 = None

    # ---------- BM25 语料 ----------

    def _ensure_bm25(self) -> BM25Index:
        if self._bm25 is None:
            vs = get_vectorstore(settings=self.settings)
            raw = vs.get(include=["documents", "metadatas"])
            chunks = []
            for text, meta in zip(raw["documents"], raw["metadatas"]):
                meta = meta or {}
                doc_id = str(meta.get("doc_id", ""))
                chunks.append({
                    "key": _chunk_key(doc_id, meta, text),
                    "doc_id": doc_id,
                    "text": text,
                    "metadata": meta,
                })
            self._bm25 = BM25Index(chunks)
        return self._bm25

    # ---------- 主入口 ----------

    def retrieve(self, question: str, top_k: int | None = None) -> tuple[list[tuple[Document, float]], list[TraceStep]]:
        """检索管道入口，返回 (子块命中列表, 检索阶段的 span 列表)

        返回的 Document 是子块（metadata 含父块信息），由 pipeline 统一组装父块。
        """
        s = self.settings
        top_k = top_k or s.top_k
        fetch_k = max(12, top_k * 3)
        spans: list[TraceStep] = []
        t0 = time.perf_counter()

        candidates: dict[str, tuple[Document, float]] = {}  # key -> (doc, best_score)

        # ---- 向量召回 ----
        t = time.perf_counter()
        vs = get_vectorstore(settings=s)
        vec_hits = vs.similarity_search_with_relevance_scores(question, k=fetch_k)
        spans.append(TraceStep(
            stage="vector_search", start_ms=(t - t0) * 1000,
            cost_ms=(time.perf_counter() - t) * 1000, detail={"hits": len(vec_hits), "k": fetch_k},
        ))
        for doc, score in vec_hits:
            meta = doc.metadata
            key = _chunk_key(str(meta.get("doc_id", "")), meta, doc.page_content)
            candidates[key] = (doc, float(score))

        # ---- BM25 召回 + 融合 ----
        if s.hybrid_enabled and s.bm25_enabled:
            t = time.perf_counter()
            bm25 = self._ensure_bm25()
            bm_hits = bm25.search(question, fetch_k)
            spans.append(TraceStep(
                stage="bm25_search", start_ms=(t - t0) * 1000,
                cost_ms=(time.perf_counter() - t) * 1000, detail={"hits": len(bm_hits), "k": fetch_k},
            ))

            t = time.perf_counter()
            if s.fusion == "weighted":
                # 加权线性组合：向量相关性 0-1 已归一，BM25 分数做最大值归一
                fused = self._fuse_weighted(vec_hits, bm_hits, candidates, alpha=0.6)
            else:
                fused = self._fuse_rrf(vec_hits, bm_hits, candidates)
            spans.append(TraceStep(
                stage="fusion", start_ms=(t - t0) * 1000,
                cost_ms=(time.perf_counter() - t) * 1000,
                detail={"strategy": s.fusion, "candidates": len(fused)},
            ))
            ranked = fused
        else:
            ranked = sorted(candidates.values(), key=lambda x: x[1], reverse=True)

        # ---- 重排序 ----
        if s.rerank != "none" and ranked:
            t = time.perf_counter()
            # 只对融合后前 fetch_k 个候选重排，控制 CPU 推理成本
            ranked = ranked[: max(fetch_k, top_k)]
            ranked = self._rerank(question, ranked)
            spans.append(TraceStep(
                stage="rerank", start_ms=(t - t0) * 1000,
                cost_ms=(time.perf_counter() - t) * 1000,
                detail={"backend": s.rerank, "model": s.rerank_model if s.rerank == "cross_encoder" else "-",
                        "candidates": len(ranked), "kept": min(s.rerank_top_n, len(ranked))},
            ))
            ranked = ranked[: s.rerank_top_n]

        # ---- 压缩过滤 ----
        if s.compress_threshold > 0 and ranked:
            t = time.perf_counter()
            ranked = self._compress(question, ranked)
            spans.append(TraceStep(
                stage="compress", start_ms=(t - t0) * 1000,
                cost_ms=(time.perf_counter() - t) * 1000,
                detail={"threshold": s.compress_threshold, "kept": len(ranked)},
            ))

        ranked = ranked[:top_k]
        return ranked, spans

    # ---------- 融合策略 ----------

    @staticmethod
    def _fuse_rrf(vec_hits, bm_hits, candidates, k: int = 60):
        """RRF：score = Σ 1/(rank + 60)，只看排名不看原始分数（教程同款）"""
        scores: dict[str, float] = {}
        for rank, (doc, _) in enumerate(vec_hits):
            meta = doc.metadata
            key = _chunk_key(str(meta.get("doc_id", "")), meta, doc.page_content)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank + 1 + k)
        for rank, (chunk, _) in enumerate(bm_hits):
            scores[chunk["key"]] = scores.get(chunk["key"], 0.0) + 1.0 / (rank + 1 + k)

        fused = []
        for key, rrf_score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            if key in candidates:
                doc, vec_score = candidates[key]
                fused.append((doc, rrf_score))
            else:
                for chunk, _ in bm_hits:
                    if chunk["key"] == key:
                        fused.append((
                            Document(page_content=chunk["text"], metadata=dict(chunk["metadata"])),
                            rrf_score,
                        ))
                        break
        return fused

    @staticmethod
    def _fuse_weighted(vec_hits, bm_hits, candidates, alpha: float = 0.6):
        """加权线性组合：α·归一化向量分 + (1-α)·归一化 BM25 分"""
        bm_scores = {c["key"]: s for c, s in bm_hits}
        bm_max = max(bm_scores.values(), default=1.0) or 1.0

        fused = []
        for key, (doc, vec_score) in candidates.items():
            bm_norm = bm_scores.get(key, 0.0) / bm_max
            fused.append((doc, alpha * vec_score + (1 - alpha) * bm_norm))
        fused.sort(key=lambda x: x[1], reverse=True)
        return fused

    # ---------- 重排序 ----------

    def _rerank(self, question: str, ranked: list[tuple[Document, float]]):
        backend = self.settings.rerank
        if backend == "cross_encoder":
            model = _get_reranker(self.settings.rerank_model)
            pairs = [[question, doc.page_content] for doc, _ in ranked]
            scores = model.predict(pairs)
            rescored = [(doc, float(s)) for (doc, _), s in zip(ranked, scores)]
            rescored.sort(key=lambda x: x[1], reverse=True)
            return rescored
        if backend == "llm":
            return self._rerank_llm(question, ranked)
        return ranked  # "rrf" 或其他：仅保留融合排序

    def _rerank_llm(self, question: str, ranked):
        """LLM 打分重排（0-10 相关性），慢，仅高价值场景"""
        import asyncio

        from dot.ai.types import UserMessage

        provider = OpenAIProvider()

        async def _score_one(doc_text: str) -> float:

            chunks = []
            try:
                async for ev in provider.stream_response(
                    model=self.settings.resolve_generate_model(),
                    system="你是相关性评估器。只输出 0-10 的整数分数，10 表示完全相关。",
                    messages=[UserMessage(content=f"问题：{question}\n\n文档：{doc_text[:500]}")],
                    tools=[],
                ):
                    if isinstance(ev, TextDeltaEvent):
                        chunks.append(ev.delta)
                return max(0.0, min(10.0, float("".join(chunks).strip() or 0)))
            except Exception:
                return 5.0

        scores = asyncio.run(asyncio.gather(*[_score_one(d.page_content) for d, _ in ranked]))
        rescored = [(doc, s) for (doc, _), s in zip(ranked, scores)]
        rescored.sort(key=lambda x: x[1], reverse=True)
        return rescored

    # ---------- 压缩 ----------

    def _compress(self, question: str, ranked):
        from .embeddings import get_embeddings

        emb = get_embeddings(self.settings)
        qv = emb.embed_query(question)
        texts = [doc.page_content for doc, _ in ranked]
        dvs = emb.embed_documents(texts)
        kept = []
        for (doc, score), dv in zip(ranked, dvs):
            sim = sum(a * b for a, b in zip(qv, dv))  # 已归一化，点积即余弦
            if sim >= self.settings.compress_threshold:
                kept.append((doc, score))
        return kept
