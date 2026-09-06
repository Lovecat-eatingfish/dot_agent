"""
dot.rag.vectorstore — 向量库层（LangChain 统一接口 + 后端工厂）

不重复造抽象：LangChain 的 VectorStore / Retriever 接口本身就是跨库统一的
（add_documents / similarity_search_with_score / as_retriever / delete），
本文件只负责按配置（RAG_VECTOR_BACKEND）选择具体实现。

对应 RAG 全链路中的「索引构建」环节。
"""
from __future__ import annotations

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore

from .config import RagSettings
from .embeddings import get_embeddings


def _create_chroma(embeddings: Embeddings, settings: RagSettings) -> VectorStore:
    from pathlib import Path

    persist_dir = Path(settings.vector_db_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)

    from langchain_chroma import Chroma

    return Chroma(
        collection_name=settings.collection_name,
        embedding_function=embeddings,
        persist_directory=str(persist_dir),
    )


def _create_milvus(embeddings: Embeddings, settings: RagSettings) -> VectorStore:
    try:
        from langchain_milvus import Milvus
    except ImportError as exc:  # milvus-lite 无 Windows 版，按需安装
        raise ImportError("使用 milvus 后端请先: uv add langchain-milvus") from exc

    return Milvus(
        embedding_function=embeddings,
        collection_name=settings.collection_name,
        connection_args={"uri": settings.vector_db_dir},
        auto_id=True,
    )


_VECTOR_BACKENDS = {
    "chroma": _create_chroma,
    "milvus": _create_milvus,
}


def get_vectorstore(
    embeddings: Embeddings | None = None, settings: RagSettings | None = None
) -> VectorStore:
    """按配置连接向量库（RAG_VECTOR_BACKEND，默认 chroma）"""
    settings = settings or RagSettings()
    embeddings = embeddings or get_embeddings(settings)

    backend = settings.vector_backend.lower()
    creator = _VECTOR_BACKENDS.get(backend)
    if creator is None:
        raise ValueError(f"未知的向量库后端: {backend}，可选: {sorted(_VECTOR_BACKENDS)}")
    return creator(embeddings, settings)


def ingest_documents(
    documents: list[Document],
    embeddings: Embeddings | None = None,
    settings: RagSettings | None = None,
) -> int:
    """文档向量化并写入向量库，返回写入的 chunk 数"""
    if not documents:
        return 0
    store = get_vectorstore(embeddings, settings)
    store.add_documents(documents)
    return len(documents)


def delete_by_doc(doc_id: str, settings: RagSettings | None = None) -> None:
    """按 doc_id 删除某文档的全部向量（文档删除/重新入库时用）

    where 过滤是 LangChain VectorStore 通用能力（Chroma/Milvus 均支持），
    不依赖任何具体实现的私有 API。
    """
    settings = settings or RagSettings()
    store = get_vectorstore(settings=settings)
    store.delete(where={"doc_id": {"$eq": doc_id}})


def get_document_chunks(doc_id: str, settings: RagSettings | None = None) -> list[dict]:
    """取出某文档实际入库的全部 chunk（按父块顺序 + 子块顺序排序）

    返回 [{"parent_id", "parent_index", "parent_text", "chunk_index", "text", "source"}, ...]
    注意：按 metadata 过滤取回是 Chroma 的能力（.get），换向量库后端时需对应实现。
    """
    settings = settings or RagSettings()
    store = get_vectorstore(settings=settings)
    raw = store.get(where={"doc_id": {"$eq": doc_id}})

    chunks: list[dict] = []
    for text, meta in zip(raw["documents"], raw["metadatas"]):
        meta = meta or {}
        chunks.append({
            "parent_id": meta.get("parent_id", ""),
            "parent_index": meta.get("parent_index", 0),
            "parent_text": meta.get("parent_text", text),
            "chunk_index": meta.get("chunk_index", 0),
            "text": text,
            "source": meta.get("source", ""),
        })

    import re

    def _parent_order(c: dict) -> int:
        # 优先用 parent_id 里的数字（p0/p1/p12 自然排序）；
        # 旧数据没有该字段时退回 parent_index
        m = re.search(r"\d+", c["parent_id"] or "")
        return int(m.group()) if m else c["parent_index"]

    chunks.sort(key=lambda c: (_parent_order(c), c["chunk_index"]))
    return chunks


def get_retriever(
    top_k: int | None = None,
    embeddings: Embeddings | None = None,
    settings: RagSettings | None = None,
) -> BaseRetriever:
    """获取检索器（相似度检索 top_k 个 chunk）"""
    settings = settings or RagSettings()
    store = get_vectorstore(embeddings, settings)
    return store.as_retriever(search_kwargs={"k": top_k or settings.top_k})
