"""本地文件后端：ChromaDB（child 向量）+ JSON（parent 原文 + doc 版本）

默认实现 StoreBackend。设计要点：
- child 向量 + metadata 落 ChromaDB（collection mokioclaw_rag，向后兼容现有数据）
- parent 原文 + doc 版本记录落 JSON 文件：parents/{doc_id}.json
  按 doc_id 分文件，避免并发写竞争，原子写用 core/utils.write_json
- version 管理：add_chunks 时读当前 version，+1 写入，旧 Chroma 记录标记 deleted=True
- 逻辑删除：delete_doc 标记 deleted=True（不物理删除，保留可审计）
- 查询过滤：query/get_children 自动注入 version=max AND deleted=False

阶段 1（兼容）：当 splitter 还没产出真正的 parent/child 时，用「parent=child」
退化模式——每个 chunk 既是 parent 又是 child，行为等价旧的 ChromaStore。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_rag_dir
from mokioclaw.core.utils import utc_now, write_json
from mokioclaw.rag.embedding import Embedder
from mokioclaw.rag.security import sanitize_doc_id
from mokioclaw.rag.types import ChildChunk, DocRecord, ParentChunk

logger = get_logger(__name__)

_COLLECTION_NAME = "mokioclaw_rag"
# parent_id 格式：{safe_doc_id}#p{index}  （避免 split(":p") 与 url/file doc_id 冲突）
_PARENT_SEP = "#p"
_CHILD_SEP = "#c"


class LocalFileBackend:
    """本地文件存储后端：ChromaDB 向量 + JSON parent 原文

    实现 StoreBackend Protocol。构造期绑定 embedder + Chroma collection。
    """

    def __init__(
        self,
        embedder: Embedder,
        persist_dir: Path | None = None,
    ) -> None:
        self.embedder = embedder
        self.persist_dir = persist_dir or (default_rag_dir() / "chroma")
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._parents_dir = self.persist_dir.parent / "parents"
        self._parents_dir.mkdir(parents=True, exist_ok=True)
        # 延迟 import：chromadb 较重
        from langchain_chroma import Chroma

        self._vectorstore = Chroma(
            collection_name=_COLLECTION_NAME,
            embedding_function=_LangChainEmbedderAdapter(embedder),
            persist_directory=str(self.persist_dir),
        )

    # ------------------------------------------------------------------
    # StoreBackend 实现
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        parents: list[ParentChunk],
        children: list[ChildChunk],
    ) -> int:
        """批量入库：version+1 + 旧版本逻辑删除 + 写 parent JSON + child upsert

        阶段1 退化模式：parents 为空时用 children 自身做 parent（parent=child）。
        """
        if not children:
            return 0
        # 净化 doc_id，防止 parents 路径穿越
        raw_doc_id = children[0].doc_id
        doc_id = sanitize_doc_id(raw_doc_id)
        if doc_id != raw_doc_id:
            for c in children:
                c.doc_id = doc_id
            for p in parents:
                p.doc_id = doc_id
        # 当前版本 → 新版本号
        new_version = self._current_version(doc_id) + 1
        # 旧版本逻辑删除：Chroma 用 raw_doc_id 查询（旧记录存的是原始值），parent JSON 用 safe_doc_id
        self._mark_old_versions_deleted(raw_doc_id, doc_id, new_version)

        # parents 为空：退化模式，用每个 child 自身当 parent
        if not parents:
            parents = []
            for i, c in enumerate(children):
                pid = make_parent_id(doc_id, i)
                parents.append(ParentChunk(
                    parent_id=pid,
                    doc_id=doc_id,
                    content=c.content,
                    metadata=dict(c.metadata),
                    version=new_version,
                ))
                c.parent_id = pid
                c.child_id = make_child_id(doc_id, i)
                c.doc_id = doc_id
        else:
            # 规范化已有 parent/child id
            for i, p in enumerate(parents):
                p.doc_id = doc_id
                if not p.parent_id or ":p" in p.parent_id and _PARENT_SEP not in p.parent_id:
                    p.parent_id = make_parent_id(doc_id, i)
            for i, c in enumerate(children):
                c.doc_id = doc_id
                if not c.child_id or (":c" in c.child_id and _CHILD_SEP not in c.child_id):
                    c.child_id = make_child_id(doc_id, i)
                # parent_id 优先沿用对应 parent
                if i < len(parents):
                    c.parent_id = parents[i].parent_id
                elif not c.parent_id:
                    c.parent_id = make_parent_id(doc_id, 0)

        # 统一刷 version
        for p in parents:
            p.version = new_version
        for c in children:
            c.version = new_version

        # 写 parent JSON（按 doc_id 汇总，追加版本记录）
        self._write_parents(doc_id, parents, new_version)

        # child 落 Chroma（带完整 metadata）
        metadatas = [self._child_metadata(c) for c in children]
        ids = [c.child_id for c in children]
        texts = [c.content for c in children]
        self._vectorstore.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        logger.info(
            "rag backend: ingested %d children (v%d) for doc_id=%s",
            len(children), new_version, doc_id,
        )
        return len(children)

    def query_children(
        self,
        text: str,
        k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[ChildChunk]:
        """向量检索 top-k child（自动过滤 deleted=False）

        version 过滤：不限制具体版本，靠 deleted=False 排除旧版本
        （旧版本在 add_chunks 时已被标记 deleted=True）。
        """
        if not text.strip():
            return []
        base_filter: dict[str, Any] = {"deleted": False}
        if where:
            base_filter = {"$and": [base_filter, where]}
        docs = self._vectorstore.similarity_search(text, k=k, filter=base_filter)
        results: list[ChildChunk] = []
        for d in docs:
            m = dict(d.metadata) if d.metadata else {}
            results.append(ChildChunk(
                child_id=str(m.get("child_id", "")),
                doc_id=str(m.get("doc_id", "")),
                parent_id=str(m.get("parent_id", "")),
                content=d.page_content,
                metadata=m,
                version=int(m.get("version", 1)),
                deleted=False,
            ))
        return results

    def get_parent(self, parent_id: str) -> ParentChunk | None:
        """按 parent_id 取父块原文（读 parent JSON，跳过 deleted）"""
        if not parent_id:
            return None
        doc_id = doc_id_from_parent_id(parent_id)
        if not doc_id:
            # 兼容旧格式 doc:p0 —— 仅当能安全提取时
            if ":p" in parent_id and _PARENT_SEP not in parent_id:
                # 旧格式不可靠，尝试从 metadata 全表扫（小体量）
                return self._find_parent_scan(parent_id)
            return None
        records = self._read_parents(doc_id)
        for p in records:
            if p.parent_id == parent_id and not p.deleted:
                return p
        return None

    def _find_parent_scan(self, parent_id: str) -> ParentChunk | None:
        """兼容旧 parent_id：在 parents 目录扫描匹配（仅 fallback）"""
        try:
            for path in self._parents_dir.glob("*.json"):
                import json
                data = json.loads(path.read_text(encoding="utf-8"))
                for rec in data.get("parents", []) or []:
                    if rec.get("parent_id") == parent_id and not rec.get("deleted"):
                        return ParentChunk(
                            parent_id=rec["parent_id"],
                            doc_id=rec.get("doc_id", ""),
                            content=rec.get("content", ""),
                            metadata=rec.get("metadata", {}),
                            version=rec.get("version", 1),
                            deleted=False,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.debug("parent scan failed: %s", exc)
        return None

    def delete_doc(self, doc_id: str) -> None:
        """逻辑删除：标记 Chroma 记录 deleted=True + parent JSON deleted=True"""
        if not doc_id:
            return
        safe_doc_id = sanitize_doc_id(doc_id)
        # Chroma: 兼容 raw 和 sanitized doc_id
        for query_id in (doc_id, safe_doc_id) if doc_id != safe_doc_id else (doc_id,):
            try:
                col = self._vectorstore._collection
                existing = col.get(where={"doc_id": query_id}, include=["metadatas"])
                ids = existing.get("ids", [])
                metas = existing.get("metadatas", [])
                if ids:
                    new_metas = [
                        {**(m or {}), "deleted": True} for m in metas
                    ]
                    col.update(ids=ids, metadatas=new_metas)
            except Exception as exc:  # noqa: BLE001
                logger.debug("rag delete_doc chroma '%s' failed: %s", query_id, exc)
        # parent JSON：使用 sanitized doc_id 路径
        records = self._read_parents(safe_doc_id)
        for p in records:
            p.deleted = True
        self._write_parent_records(safe_doc_id, records, mark_deleted=True)

    def list_docs(self) -> list[DocRecord]:
        """列出未删除文档（按 doc_id 聚合，返回当前 version）"""
        try:
            results = self._vectorstore._collection.get(include=["metadatas"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag list_docs failed: %s", exc)
            return []
        seen: dict[str, DocRecord] = {}
        for meta in results.get("metadatas", []):
            if not isinstance(meta, dict) or meta.get("deleted"):
                continue
            did = str(meta.get("doc_id", ""))
            if not did:
                continue
            version = int(meta.get("version", 1))
            rec = seen.get(did)
            if rec is None:
                seen[did] = DocRecord(
                    doc_id=did,
                    source=str(meta.get("source", "")),
                    version=version,
                    chunk_count=1,
                    updated_at="",
                )
            else:
                rec.chunk_count += 1
                if version > rec.version:
                    rec.version = version
        return list(seen.values())

    def get_children_by_doc(self, doc_id: str) -> list[ChildChunk]:
        """取某 doc 当前未删除的所有 child（供 BM25 重建索引）"""
        try:
            results = self._vectorstore._collection.get(
                where={"doc_id": doc_id}, include=["metadatas", "documents"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag get_children_by_doc '%s' failed: %s", doc_id, exc)
            return []
        out: list[ChildChunk] = []
        ids = results.get("ids", [])
        metas = results.get("metadatas", [])
        docs = results.get("documents", [])
        for cid, m, doc in zip(ids, metas, docs):
            if not isinstance(m, dict) or m.get("deleted"):
                continue
            out.append(ChildChunk(
                child_id=str(cid),
                doc_id=str(m.get("doc_id", doc_id)),
                parent_id=str(m.get("parent_id", "")),
                content=doc or "",
                metadata=dict(m),
                version=int(m.get("version", 1)),
            ))
        return out

    # ------------------------------------------------------------------
    # 内部：版本管理 + parent JSON
    # ------------------------------------------------------------------

    def _current_version(self, doc_id: str) -> int:
        """读 parent JSON 的当前 max version（无记录返回 0）"""
        records = self._read_parents(doc_id)
        if not records:
            return 0
        return max((p.version for p in records), default=0)

    def _mark_old_versions_deleted(self, raw_doc_id: str, safe_doc_id: str, new_version: int) -> None:
        """逻辑删除同 doc_id 的旧版本（Chroma metadata update + parent JSON 标记）

        raw_doc_id: 用于 Chroma 查询（旧记录存的是未 sanitize 的值）
        safe_doc_id: 用于 parent JSON 路径（已 sanitize）
        """
        try:
            col = self._vectorstore._collection
            existing = col.get(where={"doc_id": raw_doc_id}, include=["metadatas"])
            ids = existing.get("ids", [])
            metas = existing.get("metadatas", [])
            if ids:
                new_metas = [
                    {**(m or {}), "deleted": True} for m in metas
                ]
                col.update(ids=ids, metadatas=new_metas)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag mark_old_versions '%s' failed: %s", raw_doc_id, exc)
        # parent JSON 旧记录也标记 deleted
        records = self._read_parents(safe_doc_id)
        for p in records:
            p.deleted = True
        if records:
            self._write_parent_records(safe_doc_id, records, mark_deleted=False)

    def _parents_path(self, doc_id: str) -> Path:
        """将已 sanitize 的 doc_id 转为 parent JSON 路径（调用方保证 doc_id 已净化）"""
        path = (self._parents_dir / f"{doc_id}.json").resolve()
        # 双重保险：必须仍在 parents_dir 下
        parents_root = self._parents_dir.resolve()
        if not str(path).startswith(str(parents_root)) or path.parent != parents_root:
            raise ValueError(f"doc_id escapes parents dir: {doc_id!r}")
        return path

    def _read_parents(self, doc_id: str) -> list[ParentChunk]:
        path = self._parents_path(doc_id)
        if not path.exists():
            return []
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag read parents '%s' failed: %s", doc_id, exc)
            return []
        out: list[ParentChunk] = []
        for rec in data.get("parents", []):
            out.append(ParentChunk(
                parent_id=rec["parent_id"],
                doc_id=rec.get("doc_id", doc_id),
                content=rec.get("content", ""),
                metadata=rec.get("metadata", {}),
                version=rec.get("version", 1),
                deleted=rec.get("deleted", False),
            ))
        return out

    def _write_parents(
        self,
        doc_id: str,
        parents: list[ParentChunk],
        version: int,
    ) -> None:
        """写 parent JSON（保留旧版本记录，追加新版本）"""
        records = self._read_parents(doc_id)
        records.extend(parents)
        self._write_parent_records(doc_id, records, mark_deleted=False)

    def _write_parent_records(
        self,
        doc_id: str,
        records: list[ParentChunk],
        *,
        mark_deleted: bool,
    ) -> None:
        if mark_deleted:
            for p in records:
                p.deleted = True
        payload = {
            "doc_id": doc_id,
            "updated_at": utc_now(),
            "parents": [
                {
                    "parent_id": p.parent_id,
                    "doc_id": p.doc_id,
                    "content": p.content,
                    "metadata": p.metadata,
                    "version": p.version,
                    "deleted": p.deleted,
                }
                for p in records
            ],
        }
        write_json(self._parents_path(doc_id), payload)

    @staticmethod
    def _child_metadata(c: ChildChunk) -> dict[str, Any]:
        """构造 Chroma metadata（含过滤字段 version/deleted/parent_id）"""
        m = dict(c.metadata)
        m.update({
            "doc_id": c.doc_id,
            "child_id": c.child_id,
            "parent_id": c.parent_id,
            "version": c.version,
            "deleted": c.deleted,
        })
        return m


class _LangChainEmbedderAdapter:
    """把 Embedder Protocol 适配为 langchain Embeddings 接口

    langchain-chroma 需要的对象有 embed_documents / embed_query 两个方法。
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embedder.embed_texts(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embedder.embed_query(text)


def make_parent_id(doc_id: str, index: int) -> str:
    return f"{sanitize_doc_id(doc_id)}{_PARENT_SEP}{int(index)}"


def make_child_id(doc_id: str, index: int) -> str:
    return f"{sanitize_doc_id(doc_id)}{_CHILD_SEP}{int(index)}"


def doc_id_from_parent_id(parent_id: str) -> str:
    """从 parent_id 提取 doc_id（新格式 {doc}#p{n}）"""
    if _PARENT_SEP not in parent_id:
        return ""
    return parent_id.rsplit(_PARENT_SEP, 1)[0]
