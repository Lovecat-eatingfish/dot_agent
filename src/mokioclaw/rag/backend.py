"""存储后端抽象（StoreBackend Protocol）— 「可换数据库实现」的唯一契约

设计原则（对齐项目既有 Embedder Protocol 风格）：
- Protocol（结构性子类型），实现方无需继承，鸭子类型
- 接口尽量薄——只有业务真正需要的方法，不预设任何后端特性
- 不泄漏 embedding/embedder：embedder 是具体实现的构造参数，不在 Protocol 上
- version/deleted 过滤由实现负责（query/get_children 只返回 max version 且未删除）

将来换 Postgres / ES 只需实现这 6 个方法，业务层（retriever/service）零改动。
"""
from __future__ import annotations

from typing import Any, Protocol

from mokioclaw.rag.types import ChildChunk, DocRecord, ParentChunk


class StoreBackend(Protocol):
    """存储后端抽象契约

    所有方法对 version/deleted 的处理由实现负责：
    - add_chunks 写入时自动 version+1 并标记同 doc_id 旧版本 deleted=True
    - query_children / get_children_by_doc 只返回 max(version) 且 deleted=False
    """

    def add_chunks(
        self,
        parents: list[ParentChunk],
        children: list[ChildChunk],
    ) -> int:
        """批量入库。同 doc_id 自动版本递增 + 旧版本逻辑删除。

        Returns: 实际写入的 child 数
        """
        ...

    def query_children(
        self,
        text: str,
        k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[ChildChunk]:
        """向量检索 top-k child chunks（已过滤 version/deleted）"""
        ...

    def get_parent(self, parent_id: str) -> ParentChunk | None:
        """按 parent_id 取父块原文（已过滤 deleted）"""
        ...

    def delete_doc(self, doc_id: str) -> None:
        """逻辑删除某 doc 的所有版本（标记 deleted=True）"""
        ...

    def list_docs(self) -> list[DocRecord]:
        """列出未删除文档（按 doc_id 聚合，返回当前 version）"""
        ...

    def get_children_by_doc(self, doc_id: str) -> list[ChildChunk]:
        """取某 doc 当前版本的所有 child（供 BM25 重建索引）

        只返回当前 max(version) 未删除的。
        """
        ...
