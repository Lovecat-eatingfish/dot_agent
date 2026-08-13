"""RAG 模块：面向 Web 端的全流程检索增强生成基础设施

全流程：文档接入(file/text/url) → 解析 → 结构感知分割 → embedding → ChromaDB 向量存储 → 检索

对齐 RAG 演进：分割采用「结构感知优先 + 递归字符降级」而非朴素固定字符数切分。
"""
from __future__ import annotations

from mokioclaw.rag.splitter import Chunk, StructureAwareSplitter
from mokioclaw.rag.store import ChromaStore
from mokioclaw.rag.backend import StoreBackend
from mokioclaw.rag.backends.local_file import LocalFileBackend
from mokioclaw.rag.types import ChildChunk, DocRecord, ParentChunk
from mokioclaw.rag.cache import CacheBackend, LocalFileCache

__all__ = [
    "Chunk",
    "StructureAwareSplitter",
    "ChromaStore",
    "StoreBackend",
    "LocalFileBackend",
    "ParentChunk",
    "ChildChunk",
    "DocRecord",
    "CacheBackend",
    "LocalFileCache",
]
