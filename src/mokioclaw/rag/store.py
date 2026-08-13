"""ChromaStore 兼容门面 — 委托 LocalFileBackend

保留 ChromaStore 类名（service/tests 直接引用），内部委托 LocalFileBackend。
高级能力（父子分块/混合检索/rerank/trace）通过新模块 opt-in，ChromaStore 保持
向后兼容的 add/query/delete_doc/list_docs 语义。

add 仍接收 list[Chunk]（splitter 产出），内部转成「parent=child」退化模式
交给 LocalFileBackend，行为等价旧实现（阶段1 兼容）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mokioclaw.core.paths import default_rag_dir
from mokioclaw.rag.backends.local_file import LocalFileBackend
from mokioclaw.rag.embedding import Embedder
from mokioclaw.rag.splitter import Chunk
from mokioclaw.rag.types import ChildChunk, ParentChunk


class ChromaStore:
    """ChromaDB 封装门面：委托 LocalFileBackend

    Args:
        embedder: embedding 后端（create_embedder() 或 FakeEmbedder）
        persist_dir: 持久化目录，默认 <root>/.mokioclaw/rag/chroma
    """

    def __init__(
        self,
        embedder: Embedder,
        persist_dir: Path | None = None,
    ) -> None:
        self.embedder = embedder
        self.persist_dir = persist_dir or (default_rag_dir() / "chroma")
        self._backend = LocalFileBackend(embedder=embedder, persist_dir=persist_dir)

    @property
    def backend(self) -> LocalFileBackend:
        """暴露底层 backend（retriever/service 直接用）"""
        return self._backend

    def add(self, chunks: list[Chunk]) -> int:
        """批量入库 chunks（带元数据）

        退化模式：每个 chunk 既是 parent 又是 child（阶段1 兼容，行为等价旧实现）。
        Returns: 实际写入的 chunk 数
        """
        if not chunks:
            return 0
        doc_id = chunks[0].doc_id
        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        for i, c in enumerate(chunks):
            pid = f"{doc_id}:p{i}"
            cid = f"{doc_id}:c{i}"
            meta = dict(c.metadata)
            parents.append(ParentChunk(
                parent_id=pid, doc_id=doc_id, content=c.content, metadata=dict(meta),
            ))
            children.append(ChildChunk(
                child_id=cid, doc_id=doc_id, parent_id=pid,
                content=c.content, metadata=meta,
            ))
        return self._backend.add_chunks(parents, children)

    def query(
        self,
        text: str,
        k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """向量检索 top-k 相关 chunks（返回 Chunk 兼容旧接口）"""
        children = self._backend.query_children(text, k=k, where=where)
        return [
            Chunk(content=c.content, metadata=c.metadata)
            for c in children
        ]

    def delete_doc(self, doc_id: str) -> None:
        """逻辑删除某文档的所有版本"""
        self._backend.delete_doc(doc_id)

    def list_docs(self) -> list[dict[str, Any]]:
        """列出已入库文档（兼容旧 dict 返回）"""
        return [
            {
                "doc_id": r.doc_id,
                "source": r.source,
                "chunk_count": r.chunk_count,
                "version": r.version,
            }
            for r in self._backend.list_docs()
        ]
