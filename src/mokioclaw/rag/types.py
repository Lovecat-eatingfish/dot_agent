"""RAG 数据模型 — 父子分块 + 文档版本记录

脱离对 splitter 的反向依赖：splitter 产出通用 Chunk，入库时由 backend 层
转成 ParentChunk / ChildChunk。这样 splitter 不需要知道存储结构。

设计要点（对齐高级 RAG）：
- ParentChunk：大段完整原文，不向量化，保存完整上下文
- ChildChunk：细粒度切片，用于向量检索，绑定 parent_id
- DocRecord：文档级元数据，带 version 和逻辑删除标记

version + deleted 解决文档更新不一致（逻辑删除旧版本，查询只取 max version）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParentChunk:
    """父块：完整原文，不向量化，查询时按 parent_id 取回提供上下文"""

    parent_id: str  # f"{doc_id}:p{index}"
    doc_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    deleted: bool = False


@dataclass
class ChildChunk:
    """子块：细粒度切片，向量化用于检索，绑定 parent_id 取回父块上下文"""

    child_id: str  # f"{doc_id}:c{index}"
    doc_id: str
    parent_id: str
    content: str  # 向量化文本
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    deleted: bool = False


@dataclass
class DocRecord:
    """文档级记录（list_docs 返回用）"""

    doc_id: str
    source: str = ""
    version: int = 1
    chunk_count: int = 0
    deleted: bool = False
    updated_at: str = ""
