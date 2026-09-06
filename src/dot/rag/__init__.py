"""
dot.rag — RAG（检索增强生成）模块

基于 LangChain 的全链路实现（数据准备 → 索引 → 检索 → 生成）：
  - Embedding：本地 HuggingFace 模型（BGE 中文）
  - 向量库：Milvus Lite（嵌入式本地文件）
  - 生成：复用 dot.ai 的 OpenAI 兼容 Provider

快速上手：
    from dot.rag import RAGPipeline
    pipeline = RAGPipeline()
    pipeline.ingest("data/rag/docs")      # 建索引
    result = await pipeline.aquery("...")  # 问答

命令行：
    uv run python -m dot.rag ingest <目录>
    uv run python -m dot.rag query "你的问题"
"""
from __future__ import annotations

from .config import RagSettings
from .embeddings import get_embeddings
from .loader import load_and_split, load_documents, load_file, split_documents, split_parent_child
from .pipeline import RagResult, RAGPipeline
from .store import MetadataStoreBase, SqliteMetadataStore, create_metadata_store
from .vectorstore import delete_by_doc, get_retriever, get_vectorstore, ingest_documents

__all__ = [
    "RagSettings",
    "RAGPipeline",
    "RagResult",
    "MetadataStoreBase",
    "SqliteMetadataStore",
    "create_metadata_store",
    "get_embeddings",
    "get_vectorstore",
    "get_retriever",
    "delete_by_doc",
    "ingest_documents",
    "load_documents",
    "load_file",
    "split_documents",
    "split_parent_child",
    "load_and_split",
]
