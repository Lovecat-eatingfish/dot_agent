"""
长期记忆检索模块（早期面试题设计稿，已弃用）

@deprecated 运行时无功能路径。长期记忆由 ``memory/topic_store.py`` 的
``TopicStore`` 实现：MEMORY.md 作为轻量索引注入 system 动态块，主题 *.md 按需
FileRead 读取，由 ``prompts/builder.py:_load_memory_index`` 装配。本模块仅保留
供 ``tests/test_memory_interview.py`` 中的面试考察点回归测试使用，新增代码请
勿引用。

面试官考察点：
- "长期记忆可以按需检索召回，具体在什么情况下需要检索？"
- 答案：意图路由判断，依赖程度高的才需要检索
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


@dataclass
class MemoryRecord:
    """记忆记录"""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)


@dataclass
class RetrievalResult:
    """检索结果"""

    records: list[MemoryRecord]
    scores: list[float]
    query: str
    duration_ms: float
    total_candidates: int = 0


class SimpleMemoryRetriever:
    """简化版记忆检索器

    @deprecated 已被 ``memory/topic_store.py:TopicStore`` 取代。TopicStore 以
    MEMORY.md 索引 + 主题文件分离模式管理长期记忆，索引注入 system 动态块，
    正文按需读取，无需独立检索器。本类仅保留供面试考察点测试。

    不使用向量数据库，而是基于关键词匹配 + TF-IDF 简化实现
    生产环境可替换为 Milvus / Pinecone / Weaviate

    面试官考察点：
    - RAG 技术栈：Embedding + 向量索引 + 检索 + Rerank
    - 余弦相似度 vs 欧氏距离
    - 按需检索（意图路由判断）
    """

    def __init__(self, storage_path: Path | str | None = None):
        self.storage_path = Path(storage_path) if storage_path else Path(".mokioclaw/memory_store.json")
        self.records: list[MemoryRecord] = []
        self._load()

    def _load(self) -> None:
        """加载记忆库"""
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                self.records = [MemoryRecord(**r) for r in data]
                logger.info("Loaded %d memory records", len(self.records))
            except Exception as exc:
                logger.warning("Failed to load memory store: %s", exc)
                self.records = []

    def _save(self) -> None:
        """持久化记忆库"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "id": r.id,
                "content": r.content,
                "metadata": r.metadata,
                "created_at": r.created_at,
                "access_count": r.access_count,
                "last_accessed": r.last_accessed,
            }
            for r in self.records
        ]
        self.storage_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, content: str, metadata: dict[str, Any] | None = None) -> str:
        """添加记忆

        Args:
            content: 记忆内容
            metadata: 元数据（来源、时间、类型等）

        Returns:
            记忆 ID
        """
        import uuid

        record_id = hashlib.sha256(content.encode()).hexdigest()[:16]
        record = MemoryRecord(
            id=record_id,
            content=content,
            metadata=metadata or {},
        )
        self.records.append(record)
        self._save()
        logger.debug("Added memory: %s", record_id)
        return record_id

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
    ) -> RetrievalResult:
        """检索相关记忆

        基于关键词重叠度的简化检索（生产环境应使用向量检索）

        Args:
            query: 查询文本
            top_k: 返回前 K 条
            min_score: 最小相似度分数

        Returns:
            检索结果
        """
        start_time = time.time()

        # 简单的关键词匹配 + 评分
        query_words = set(query.lower().split())
        scored: list[tuple[MemoryRecord, float]] = []

        for record in self.records:
            content_words = set(record.content.lower().split())
            overlap = query_words & content_words

            if not overlap:
                continue

            # Jaccard 相似度（简化版）
            score = len(overlap) / max(len(query_words | content_words), 1)

            if score >= min_score:
                scored.append((record, score))

        # 按分数排序
        scored.sort(key=lambda x: x[1], reverse=True)
        top_results = scored[:top_k]

        # 更新访问统计
        for record, _ in top_results:
            record.access_count += 1
            record.last_accessed = time.time()

        duration_ms = (time.time() - start_time) * 1000

        return RetrievalResult(
            records=[r for r, _ in top_results],
            scores=[s for _, s in top_results],
            query=query,
            duration_ms=duration_ms,
            total_candidates=len(scored),
        )

    def batch_retrieve(
        self,
        queries: list[str],
        top_k: int = 3,
    ) -> list[RetrievalResult]:
        """批量检索"""
        return [self.retrieve(q, top_k=top_k) for q in queries]

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        if not self.records:
            return {"count": 0, "empty": True}

        total_access = sum(r.access_count for r in self.records)
        return {
            "count": len(self.records),
            "total_access": total_access,
            "avg_access": total_access / len(self.records),
            "oldest": min(r.created_at for r in self.records),
            "newest": max(r.created_at for r in self.records),
        }


class IntentBasedRetrievalTrigger:
    """基于意图的检索触发器

    @deprecated 运行时不再使用。意图路由由 ``orchestration/intent.py`` +
    ``orchestration/nodes.py:intent_router_node`` 实现，长期记忆索引常驻
    system 动态块，无需独立触发器。本类仅保留供面试考察点测试。

    解决面试官问题："长期记忆检索的触发时机取决于当前问题对历史信息的依赖程度"

    实现：
    - 轻量级意图分类（关键词匹配）
    - 判断是否需要检索历史记忆
    - 控制检索频率，避免每次对话都检索
    """

    # 高依赖意图：需要检索历史
    HIGH_DEPENDENCY_INTENTS = {
        "follow_up",  # 追问/澄清
        "continuation",  # 继续之前的工作
        "reference",  # 引用之前的内容
        "comparison",  # 对比之前的结果
        "debugging",  # 调试之前的问题
    }

    # 低依赖意图：不需要检索
    LOW_DEPENDENCY_INTENTS = {
        "new_task",  # 新任务
        "simple_query",  # 简单查询
        "greeting",  # 问候
        "chit_chat",  # 闲聊
    }

    def __init__(self, retriever: SimpleMemoryRetriever | None = None):
        self.retriever = retriever or SimpleMemoryRetriever()
        self.last_retrieval_time: float = 0.0
        self.retrieval_cooldown: float = 60.0  # 冷却时间（秒）

    def should_retrieve(self, user_input: str, intent: str | None = None) -> bool:
        """判断是否需要检索历史记忆

        Args:
            user_input: 用户输入
            intent: 意图分类（如果已知）

        Returns:
            是否需要检索
        """
        # 1. 冷却检查：避免频繁检索（优先检查）
        if time.time() - self.last_retrieval_time < self.retrieval_cooldown:
            return False

        # 2. 如果有明确的意图分类
        if intent:
            if intent in self.HIGH_DEPENDENCY_INTENTS:
                return True
            if intent in self.LOW_DEPENDENCY_INTENTS:
                return False

        # 3. 无意图分类时的启发式判断
        return self._heuristic_check(user_input)

    def _heuristic_check(self, user_input: str) -> bool:
        """启发式判断是否需要检索

        检查用户输入是否包含：
        - 指代性词汇（它、那个、之前、上次）
        - 对比性词汇（和之前比、有什么区别）
        - 延续性词汇（继续、下一步、还没完成）
        """
        import re

        patterns = [
            r"(之前|上次|刚才|之前说|之前做的)",
            r"(它|那个|这个|那个东西|这件事)",
            r"(继续|下一步|接着|还没|没完成)",
            r"(和|与|跟).*(之前|上次|原来|以前).*(比|区别|不同)",
            r"(之前|上次).*(结果|输出|答案|结论)",
        ]

        user_input_lower = user_input.lower()
        for pattern in patterns:
            if re.search(pattern, user_input_lower):
                return True

        return False

    def retrieve_if_needed(
        self,
        user_input: str,
        intent: str | None = None,
        max_items: int = 3,
    ) -> str | None:
        """按需检索记忆并格式化为上下文

        Args:
            user_input: 用户输入
            intent: 意图分类
            max_items: 最多返回几条

        Returns:
            格式化的记忆上下文，如果不需要检索则返回 None
        """
        if not self.should_retrieve(user_input, intent):
            return None

        result = self.retriever.retrieve(user_input, top_k=max_items)

        if not result.records:
            return None

        self.last_retrieval_time = time.time()

        # 格式化为上下文字符串
        parts = ["[Retrieved Memories]"]
        for i, (record, score) in enumerate(zip(result.records, result.scores), 1):
            parts.append(f"{i}. (score={score:.2f}) {record.content[:200]}")

        return "\n".join(parts)
