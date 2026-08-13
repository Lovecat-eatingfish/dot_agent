"""语义缓存（CacheBackend Protocol + 本地 JSON 实现）

对齐生产级 RAG：相同/相近 query 直接返回缓存结果，省 LLM 调用。
- 命中：精确命中（query 完全一致）或语义命中（embedding 相似度超阈值）
- 降级：缓存后端不可用 → 直接 miss，不影响主流程
- 扩展：换 Redis/Memcached 只实现 CacheBackend Protocol

设计原则（减法）：
- 默认本地 JSON 文件实现（本地优先）
- 语义命中用 embedder 算余弦相似度，阈值可配
- 缓存带 TTL（过期失效），避免脏数据
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_rag_dir
from mokioclaw.rag.embedding import Embedder

logger = get_logger(__name__)

_DEFAULT_TTL = 3600  # 默认 1 小时
_DEFAULT_THRESHOLD = 0.92  # 语义命中相似度阈值


@dataclass
class CacheEntry:
    """缓存条目"""
    query: str
    answer: str
    embedding: list[float]
    created_at: float  # unix timestamp
    ttl: int
    citations: list[dict[str, Any]] = field(default_factory=list)


class CacheBackend(Protocol):
    """缓存后端抽象（可换 Redis/Memcached）"""

    def get(self, query: str, query_embedding: list[float]) -> CacheEntry | None:
        """精确或语义命中返回条目，否则 None"""
        ...

    def put(self, entry: CacheEntry) -> None:
        """写入缓存条目"""
        ...

    def clear(self) -> int:
        """清空缓存，返回清除条数"""
        ...


class LocalFileCache:
    """本地 JSON 文件缓存实现（默认）

    存 .mokioclaw/rag/cache.json，单文件 + 全量读写。
    适合中小体量；大体量换 RedisBackend。
    """

    def __init__(
        self,
        embedder: Embedder,
        cache_dir: Path | None = None,
        ttl: int = _DEFAULT_TTL,
        threshold: float = _DEFAULT_THRESHOLD,
    ) -> None:
        self.embedder = embedder
        self.ttl = ttl
        self.threshold = threshold
        self._cache_dir = cache_dir or (default_rag_dir() / "cache")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "cache.json"
        self._entries: list[CacheEntry] = self._load()

    def get(self, query: str, query_embedding: list[float]) -> CacheEntry | None:
        """精确命中（query 一致）或语义命中（余弦相似度超阈值）"""
        now = time.time()
        for e in self._entries:
            # TTL 过期跳过
            if now - e.created_at > e.ttl:
                continue
            # 精确命中
            if e.query == query:
                return e
            # 语义命中
            sim = _cosine(query_embedding, e.embedding)
            if sim >= self.threshold:
                return e
        return None

    def put(self, entry: CacheEntry) -> None:
        """写入条目（同 query 覆盖旧的）"""
        # 去重：同 query 替换
        self._entries = [e for e in self._entries if e.query != entry.query]
        self._entries.append(entry)
        # 控制缓存条数（FIFO 淘汰，避免无限增长）
        if len(self._entries) > 1000:
            self._entries = self._entries[-1000:]
        self._save()

    def clear(self) -> int:
        n = len(self._entries)
        self._entries = []
        self._save()
        return n

    def _load(self) -> list[CacheEntry]:
        if not self._cache_file.exists():
            return []
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            return [
                CacheEntry(
                    query=e["query"],
                    answer=e["answer"],
                    embedding=e["embedding"],
                    created_at=e["created_at"],
                    ttl=e.get("ttl", self.ttl),
                    citations=e.get("citations", []),
                )
                for e in data.get("entries", [])
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag cache load failed: %s", exc)
            return []

    def _save(self) -> None:
        try:
            payload = {
                "entries": [
                    {
                        "query": e.query,
                        "answer": e.answer,
                        "embedding": e.embedding,
                        "created_at": e.created_at,
                        "ttl": e.ttl,
                        "citations": e.citations,
                    }
                    for e in self._entries
                ]
            }
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_file)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag cache save failed: %s", exc)


class NoopCache:
    """空缓存（禁用语义缓存时用，所有操作 no-op）"""

    def get(self, query: str, query_embedding: list[float]) -> CacheEntry | None:
        return None

    def put(self, entry: CacheEntry) -> None:
        pass

    def clear(self) -> int:
        return 0


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
