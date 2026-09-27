"""
dot.kg.pipeline — KG RAG Pipeline

知识图谱 RAG 全链路：
  索引: 文档 → 分块 → 实体/关系抽取 → 图谱入库
  查询: 查询 → 图检索 → 上下文 → LLM 生成
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import KGSettings
from .extractor import EntityExtractor, ExtractionResult, Entity, Relation
from .graph_retriever import GraphRetriever, RetrievalResult
from .graph_store import KnowledgeGraph

logger = logging.getLogger(__name__)

__all__ = ["KGPipeline", "IndexResult", "QueryResult"]


# ============================================================
# 数据结构
# ============================================================


@dataclass
class IndexResult:
    """索引结果"""
    doc_id: str
    entities_count: int = 0
    relations_count: int = 0
    chunks_processed: int = 0


@dataclass
class QueryResult:
    """查询结果"""
    question: str
    answer: str = ""
    matched_entities: list[dict] = field(default_factory=list)
    paths: list[dict] = field(default_factory=list)
    context: str = ""
    trace_id: str = ""


# ============================================================
# KG RAG Pipeline
# ============================================================


class KGPipeline:
    """知识图谱 RAG Pipeline

    索引: ingest_file() / ingest_text() / ingest_dir()
    查询: query() / astream()
    管理: stats() / clear()
    """

    def __init__(self, settings: KGSettings | None = None):
        self.settings = settings or KGSettings()
        self.graph = KnowledgeGraph(name="kg")

        # LLM 抽取器
        self.extractor = EntityExtractor(
            api_key=self.settings.resolve_openai_key(),
            base_url=self.settings.resolve_base_url(),
            model=self.settings.resolve_extraction_model(),
        )

        # 图检索器
        self.retriever = GraphRetriever(
            graph=self.graph,
            max_hops=self.settings.max_hops,
            max_entities=self.settings.max_entities,
        )

        # 持久化路径
        self.storage_path = self.settings.storage_dir / f"{self.graph.name}.json"
        self._load_persisted()

    # ============================================================
    # 索引阶段
    # ============================================================

    def _load_persisted(self) -> None:
        """加载已持久化的图数据"""
        if self.settings.persist_graph and self.storage_path.exists():
            try:
                self.graph = KnowledgeGraph.from_json(self.storage_path)
                self.retriever = GraphRetriever(
                    graph=self.graph,
                    max_hops=self.settings.max_hops,
                    max_entities=self.settings.max_entities,
                )
                logger.info(f"Loaded persisted graph: {self.graph}")
            except Exception as e:
                logger.warning(f"Failed to load persisted graph: {e}")

    def _persist(self) -> None:
        """持久化图数据"""
        if self.settings.persist_graph:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.graph.to_json(self.storage_path)

    def ingest_file(self, path: str | Path) -> IndexResult:
        """索引文件"""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(str(path))

        text = self._load_file(path)
        return self.ingest_text(text, source=str(path))

    def ingest_text(self, text: str, source: str = "", doc_id: str | None = None) -> IndexResult:
        """索引文本"""
        doc_id = doc_id or uuid4().hex[:8]

        # 分块（简单的句子级分块）
        chunks = self._split_chunks(text)

        # 批量抽取
        result = asyncio.run(self.extractor.extract_batch(chunks))

        # 过滤低置信度
        result.entities = [
            e for e in result.entities
            if not hasattr(e, "confidence") or (hasattr(e, "confidence") and e.confidence >= self.settings.min_confidence)
        ]

        # 添加到图谱
        self.graph.add_extraction_result(result.entities, result.relations)

        # 持久化
        self._persist()

        return IndexResult(
            doc_id=doc_id,
            entities_count=len(result.entities),
            relations_count=len(result.relations),
            chunks_processed=len(chunks),
        )

    def ingest_documents(self, documents: list[str]) -> IndexResult:
        """批量索引文档"""
        all_entities: list[Entity] = []
        all_relations: list[Relation] = []
        chunks_count = 0

        for doc in documents:
            chunks = self._split_chunks(doc)
            result = asyncio.run(self.extractor.extract_batch(chunks))
            all_entities.extend(result.entities)
            all_relations.extend(result.relations)
            chunks_count += len(chunks)

        # 去重
        entity_names = set()
        unique_entities = []
        for e in all_entities:
            if e.name not in entity_names:
                unique_entities.append(e)
                entity_names.add(e.name)

        # 添加到图谱
        self.graph.add_extraction_result(unique_entities, all_relations)

        # 持久化
        self._persist()

        return IndexResult(
            doc_id=uuid4().hex[:8],
            entities_count=len(unique_entities),
            relations_count=len(all_relations),
            chunks_processed=chunks_count,
        )

    async def aingest_text(self, text: str, source: str = "") -> IndexResult:
        """异步索引文本"""
        doc_id = uuid4().hex[:8]
        chunks = self._split_chunks(text)

        result = await self.extractor.extract_batch(chunks)

        self.graph.add_extraction_result(result.entities, result.relations)
        self._persist()

        return IndexResult(
            doc_id=doc_id,
            entities_count=len(result.entities),
            relations_count=len(result.relations),
            chunks_processed=len(chunks),
        )

    def _load_file(self, path: Path) -> str:
        """加载文件内容"""
        suffix = path.suffix.lower()
        if suffix == ".txt":
            return path.read_text(encoding="utf-8")
        elif suffix == ".md":
            return path.read_text(encoding="utf-8")
        elif suffix == ".pdf":
            # 简单的 PDF 文本提取（需要 pdfplumber 或类似库）
            try:
                import pdfplumber

                with pdfplumber.open(path) as pdf:
                    return "\n".join(p.extract_text() or "" for p in pdf.pages)
            except ImportError:
                logger.warning("pdfplumber not installed, skipping PDF")
                return ""
        else:
            # 尝试纯文本读取
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return path.read_text(encoding="utf-8", errors="ignore")

    def _split_chunks(self, text: str, chunk_size: int = 500) -> list[str]:
        """简单分块（按句子）"""
        import re

        # 按句子分割
        sentences = re.split(r"[。！？.!?\n]+", text)
        chunks = []
        current = []

        for s in sentences:
            s = s.strip()
            if not s:
                continue
            current.append(s)
            if sum(len(c) for c in current) >= chunk_size:
                chunks.append("".join(current))
                current = []

        if current:
            chunks.append("".join(current))

        return [c for c in chunks if c]

    # ============================================================
    # 查询阶段
    # ============================================================

    def query(self, question: str) -> QueryResult:
        """查询"""
        trace_id = uuid4().hex[:8]

        # 图检索
        retrieval = self.retriever.retrieve(question)

        return QueryResult(
            question=question,
            matched_entities=[e.to_dict() for e in retrieval.matched_entities],
            paths=[p.__dict__ for p in retrieval.paths],
            context=retrieval.context,
            trace_id=trace_id,
        )

    async def aquery(self, question: str) -> QueryResult:
        """异步查询"""
        # 图检索（目前是同步的）
        retrieval = self.retriever.retrieve(question)

        return QueryResult(
            question=question,
            matched_entities=[e.to_dict() for e in retrieval.matched_entities],
            paths=[p.__dict__ for p in retrieval.paths],
            context=retrieval.context,
            trace_id=uuid4().hex[:8],
        )

    async def astream(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """流式查询"""
        trace_id = uuid4().hex[:8]

        # 1. 图检索
        retrieval = self.retriever.retrieve(question)
        yield {
            "type": "retrieve",
            "trace_id": trace_id,
            "entities": [e.to_dict() for e in retrieval.matched_entities],
            "paths": [p.__dict__ for p in retrieval.paths],
            "context": retrieval.context,
        }

        # 2. 生成回答
        if not self.settings.resolve_openai_key():
            yield {"type": "error", "message": "OpenAI API key not configured"}
            return

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=self.settings.resolve_openai_key(),
                base_url=self.settings.resolve_base_url(),
            )

            # 构建 prompt
            prompt = self._build_prompt(question, retrieval)

            stream = await client.chat.completions.create(
                model=self.settings.resolve_generation_model(),
                messages=[
                    {"role": "system", "content": "你是一个知识图谱问答助手。基于提供的知识图谱信息回答用户问题。"},
                    {"role": "user", "content": prompt},
                ],
                stream=True,
            )

            answer_parts = []
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    answer_parts.append(delta)
                    yield {"type": "delta", "text": delta}

            yield {
                "type": "done",
                "trace_id": trace_id,
                "answer": "".join(answer_parts),
            }

        except Exception as e:
            logger.error(f"Generation failed: {e}")
            yield {"type": "error", "message": str(e)}

    def _build_prompt(self, question: str, retrieval: RetrievalResult) -> str:
        """构建 prompt"""
        prompt_parts = [
            f"问题: {question}\n",
            "\n## 知识图谱信息\n",
            retrieval.context or "（未找到相关知识图谱信息）",
            "\n\n请基于上述知识图谱信息回答问题。如果信息不足，请说明。",
        ]
        return "".join(prompt_parts)

    # ============================================================
    # 管理
    # ============================================================

    def stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return self.graph.stats()

    def clear(self) -> None:
        """清空图谱"""
        self.graph = KnowledgeGraph(name="kg")
        self.retriever = GraphRetriever(
            graph=self.graph,
            max_hops=self.settings.max_hops,
            max_entities=self.settings.max_entities,
        )
        if self.storage_path.exists():
            self.storage_path.unlink()

    def search_entities(self, query: str) -> list[dict]:
        """搜索实体"""
        nodes = self.graph.search_nodes(query)
        return [n.to_dict() for n in nodes]

    def get_entity(self, name: str) -> dict | None:
        """获取实体详情"""
        return self.retriever.get_entity_info(name)
