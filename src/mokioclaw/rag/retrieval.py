"""混合检索 + RRF 融合（对齐高级 RAG）

流水线：
1. 向量召回（StoreBackend.query_children）—— 语义匹配
2. BM25 召回（内存关键词打分）—— 专有名词/编号/数字强召回
3. RRF 融合 —— 两路结果去重 + 重排
4. 取 parent —— 命中 child 按 parent_id 取父块原文（完整上下文）

不依赖具体 backend，只依赖 StoreBackend Protocol，存储后端可换。
BM25 用纯 Python rank_bm25（单文件无重依赖），分词用 str.split()
（中文友好性靠 child 切分时的结构感知保证，不引 jieba）。
"""
from __future__ import annotations

from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.rag.backend import StoreBackend
from mokioclaw.rag.embedding import Embedder
from mokioclaw.rag.types import ChildChunk, ParentChunk

logger = get_logger(__name__)

# RRF 常数：rank 越靠前权重越高，k 越大融合越平滑
_RRF_K = 60


def rrf_fuse(
    vector_hits: list[ChildChunk],
    bm25_hits: list[ChildChunk],
    *,
    k: int = _RRF_K,
    top_n: int = 0,
) -> list[ChildChunk]:
    """Reciprocal Rank Fusion：两路召回结果去重 + 重排

    score = Σ 1/(k + rank)，rank 从 1 开始。
    Returns: 融合后按 score 降序的 child 列表（top_n=0 表示全部）
    """
    scores: dict[str, float] = {}
    child_map: dict[str, ChildChunk] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        scores[hit.child_id] = scores.get(hit.child_id, 0.0) + 1.0 / (k + rank)
        child_map[hit.child_id] = hit
    for rank, hit in enumerate(bm25_hits, start=1):
        scores[hit.child_id] = scores.get(hit.child_id, 0.0) + 1.0 / (k + rank)
        child_map[hit.child_id] = hit

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    result = [child_map[cid] for cid, _ in ordered]
    return result[:top_n] if top_n > 0 else result


class HybridRetriever:
    """混合检索器：向量 + BM25 → RRF → 取 parent

    BM25 索引按 doc 维度懒加载，doc 变更时失效。
    依赖 StoreBackend Protocol，不依赖具体存储后端。
    """

    def __init__(
        self,
        backend: StoreBackend,
        embedder: Embedder,
        *,
        top_k: int = 5,
        use_bm25: bool = True,
    ) -> None:
        self.backend = backend
        self.embedder = embedder
        self.top_k = top_k
        self.use_bm25 = use_bm25
        # BM25 索引缓存：doc_id → (BM25, [child_id...])
        # 失效策略：doc 重新 ingest 时 version 变，下次查询自动重建
        self._bm25_cache: dict[str, tuple[Any, list[str]]] = {}
        self._bm25_versions: dict[str, int] = {}

    def retrieve(
        self,
        query: str,
        where: dict[str, Any] | None = None,
        *,
        k: int | None = None,
    ) -> list[ParentChunk]:
        """混合检索，返回去重后的父块（完整上下文）

        Args:
            k: 本次检索的 top_k（默认用实例配置）。retriever 是应用级单例，
               请求方不要直接改 self.top_k（FastAPI sync 路由跑线程池，并发请求互相覆盖）。

        Returns: 父块列表（按 RRF 融合分数降序，去重 parent_id）
        """
        if not query.strip():
            return []
        top_k = k or self.top_k

        # 1. 向量召回（多取一些给 RRF 融合）
        vector_hits = self.backend.query_children(
            query, k=top_k * 3, where=where,
        )

        # 2. BM25 召回
        bm25_hits: list[ChildChunk] = []
        if self.use_bm25:
            try:
                bm25_hits = self._bm25_retrieve(query, where=where)
            except Exception as exc:  # noqa: BLE001
                logger.warning("rag BM25 retrieve failed, degraded: %s", exc)
                bm25_hits = []

        # 3. RRF 融合
        fused = rrf_fuse(vector_hits, bm25_hits, top_n=top_k * 2)

        # 4. 取 parent（去重 parent_id）
        seen: set[str] = set()
        parents: list[ParentChunk] = []
        for child in fused:
            if child.parent_id in seen:
                continue
            parent = self.backend.get_parent(child.parent_id)
            if parent is None:
                continue
            seen.add(child.parent_id)
            parents.append(parent)
            if len(parents) >= top_k:
                break
        return parents

    # ------------------------------------------------------------------
    # BM25 内存索引
    # ------------------------------------------------------------------

    def _bm25_retrieve(
        self,
        query: str,
        where: dict[str, Any] | None = None,
        k: int | None = None,
    ) -> list[ChildChunk]:
        """BM25 关键词召回（按 doc 建索引，跨 doc 合并结果）"""
        from rank_bm25 import BM25Okapi

        n = k or self.top_k * 3
        query_tokens = query.lower().split()

        # 收集所有 doc 的 child（过滤 where）
        all_children: list[ChildChunk] = []
        # 从向量召回的 hit 里取 doc_id（避免全库扫描）
        # 注：更完整的实现应从 list_docs 拿所有 doc_id，
        # 但为控制开销，这里复用 query_children 已召回的 doc
        doc_ids = self._candidate_doc_ids(query, where)

        for doc_id in doc_ids:
            children = self._get_indexed_children(doc_id)
            all_children.extend(children)

        if not all_children:
            return []

        # 对 where 做内存二次过滤（BM25 不支持 metadata filter）
        if where:
            all_children = [
                c for c in all_children
                if self._match_where(c.metadata, where)
            ]

        corpus = [c.content.lower().split() for c in all_children]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(query_tokens)

        # 按 score 降序取 top
        ranked = sorted(
            zip(scores, all_children), key=lambda x: x[0], reverse=True,
        )
        return [child for _, child in ranked[:n]]

    def _candidate_doc_ids(
        self,
        query: str,
        where: dict[str, Any] | None,
    ) -> list[str]:
        """获取候选 doc_id 列表（从 list_docs）"""
        try:
            docs = self.backend.list_docs()
        except Exception:  # noqa: BLE001
            return []
        return [d.doc_id for d in docs]

    def _get_indexed_children(self, doc_id: str) -> list[ChildChunk]:
        """取 doc 的当前版本 child（带版本缓存失效）"""
        children = self.backend.get_children_by_doc(doc_id)
        if not children:
            return []
        current_version = max((c.version for c in children), default=1)
        cached_version = self._bm25_versions.get(doc_id)
        if cached_version == current_version and doc_id in self._bm25_cache:
            return children
        # 版本变化或首次：重建索引缓存
        self._bm25_cache[doc_id] = (None, [c.child_id for c in children])
        self._bm25_versions[doc_id] = current_version
        return children

    @staticmethod
    def _match_where(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
        """简单 metadata 匹配（where 为 {key: value} 形式）"""
        for key, value in where.items():
            if str(metadata.get(key)) != str(value):
                return False
        return True
