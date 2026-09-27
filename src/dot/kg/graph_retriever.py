"""
dot.kg.graph_retriever — 知识图谱检索器

从知识图谱中检索与查询相关的子图，并生成结构化上下文。
支持:
- 基于关键词的实体匹配
- 多跳路径检索
- 路径上下文生成
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph_store import KnowledgeGraph, GraphNode, GraphEdge

__all__ = ["GraphRetriever", "RetrievalResult"]


# ============================================================
# 数据结构
# ============================================================


@dataclass
class PathSegment:
    """路径片段"""
    source: str
    relation: str
    target: str
    weight: float
    hop: int


@dataclass
class RetrievalResult:
    """检索结果"""
    matched_entities: list[GraphNode] = field(default_factory=list)
    paths: list[PathSegment] = field(default_factory=list)
    context: str = ""  # 结构化上下文文本
    subgraph: KnowledgeGraph | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_entities": [e.to_dict() for e in self.matched_entities],
            "paths": [
                {"source": p.source, "relation": p.relation, "target": p.target, "weight": p.weight, "hop": p.hop}
                for p in self.paths
            ],
            "context": self.context,
        }


# ============================================================
# 图检索器
# ============================================================


class GraphRetriever:
    """知识图谱检索器"""

    def __init__(
        self,
        graph: KnowledgeGraph,
        max_hops: int = 2,
        max_entities: int = 50,
    ):
        self.graph = graph
        self.max_hops = max_hops
        self.max_entities = max_entities

    def retrieve(self, query: str) -> RetrievalResult:
        """检索与查询相关的子图上下文

        Args:
            query: 查询文本

        Returns:
            RetrievalResult: 包含匹配实体、路径和结构化上下文
        """
        # 1. 匹配种子实体
        seed_entities = self._match_entities(query)

        if not seed_entities:
            return RetrievalResult(context="未找到相关实体")

        # 2. BFS 扩展获取子图
        expanded = self.graph.bfs_expand(
            [e.id for e in seed_entities],
            max_hops=self.max_hops,
        )

        # 3. 构建路径
        paths = self._build_paths(seed_entities, expanded)

        # 4. 生成上下文
        context = self._build_context(seed_entities, paths)

        # 5. 构建子图
        subgraph = self.graph.get_subgraph([e.id for e in seed_entities], self.max_hops)

        return RetrievalResult(
            matched_entities=seed_entities,
            paths=paths,
            context=context,
            subgraph=subgraph,
        )

    def _match_entities(self, query: str) -> list[GraphNode]:
        """匹配查询相关的实体

        中文查询先 jieba 分词（否则 split() 只得到整句），匹配目标包括
        实体名和实体描述——"谁负责测试模块？"不含实体名，但命中描述。
        """
        query_lower = query.lower()

        # 查询词分词（jieba 缺失或无切分结果时退化为空格分词）
        query_terms = self._tokenize(query_lower)

        candidates: list[tuple[GraphNode, float]] = []

        for node in self.graph._nodes.values():
            score = 0.0
            node_id_lower = node.id.lower()
            desc_lower = (node.description or "").lower()

            # 标题匹配
            if query_lower in node_id_lower:
                score += 10.0
            elif node_id_lower in query_lower:
                score += 5.0

            # 分词匹配（实体名 + 实体描述）
            for term in query_terms:
                if term in node_id_lower:
                    score += 1.0
                if desc_lower and term in desc_lower:
                    score += 0.5

            # 类型相关性（某些类型更可能相关）
            if node.type in ["PERSON", "ORGANIZATION", "PRODUCT"]:
                score += 0.5

            if score > 0:
                candidates.append((node, score))

        # 按分数排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c[0] for c in candidates[: self.max_entities]]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """查询分词：中文用 jieba，失败退化为空格分词"""
        try:
            import jieba

            return [t for t in jieba.cut(text) if t.strip()]
        except ImportError:
            return text.split()

    def _build_paths(
        self,
        seed_entities: list[GraphNode],
        expanded: dict[str, list[tuple[GraphNode, GraphEdge, int]]],
    ) -> list[PathSegment]:
        """构建路径"""
        paths = []

        for seed in seed_entities:
            for neighbor, edge, hop in expanded.get(seed.id, []):
                if edge.relation_type.startswith("reverse_"):
                    continue
                paths.append(PathSegment(
                    source=seed.id,
                    relation=edge.relation_type,
                    target=neighbor.id,
                    weight=edge.weight,
                    hop=hop,
                ))

        # 按跳数排序
        paths.sort(key=lambda p: p.hop)
        return paths

    def _build_context(
        self,
        seed_entities: list[GraphNode],
        paths: list[PathSegment],
    ) -> str:
        """生成结构化上下文"""
        lines = ["## 知识图谱检索结果\n"]

        # 匹配的实体
        lines.append("### 匹配实体\n")
        for e in seed_entities[:10]:  # 最多显示10个
            desc = f"（{e.description}）" if e.description else ""
            lines.append(f"- **{e.id}** [{e.type}]{desc}")
        lines.append("")

        # 关系路径
        if paths:
            lines.append("### 关系路径\n")
            for p in paths[:20]:  # 最多显示20条
                hop_marker = "(多跳)" if p.hop > 1 else ""
                lines.append(f"- {p.source} --[{p.relation}]--> {p.target} {hop_marker}")
            lines.append("")

        # 结构化文本（方便 LLM 理解）
        lines.append("### 知识摘要\n")
        if seed_entities:
            lines.append(f"已知实体: {', '.join(e.id for e in seed_entities[:5])}。")

        if paths:
            # 按源实体分组
            by_source: dict[str, list[PathSegment]] = {}
            for p in paths:
                if p.source not in by_source:
                    by_source[p.source] = []
                by_source[p.source].append(p)

            facts = []
            for source, ps in by_source.items():
                targets = [f"{p.relation} {p.target}" for p in ps]
                facts.append(f"{source} 是 {', '.join(targets)}")

            if facts:
                lines.append("；".join(facts[:5]))  # 最多5条事实

        return "\n".join(lines)

    def get_entity_info(self, entity_name: str) -> dict[str, Any] | None:
        """获取实体详细信息"""
        node = self.graph.get_node(entity_name)
        if not node:
            return None

        neighbors = self.graph.get_neighbors(entity_name)
        relations = []
        for neighbor, edge in neighbors:
            if not edge.relation_type.startswith("reverse_"):
                relations.append({
                    "target": neighbor.id,
                    "type": neighbor.type,
                    "relation": edge.relation_type,
                    "weight": edge.weight,
                })

        return {
            "entity": node.to_dict(),
            "relations": relations,
        }

    def find_paths(self, source: str, target: str, max_depth: int = 3) -> list[list[str]]:
        """查找两个实体之间的所有路径（简单 DFS）"""
        if not self.graph.has_node(source) or not self.graph.has_node(target):
            return []

        paths: list[list[str]] = []

        def dfs(current: str, target: str, path: list[str], visited: set[str]):
            if current == target:
                paths.append(path.copy())
                return
            if len(path) >= max_depth:
                return

            for neighbor, edge in self.graph.get_neighbors(current):
                if neighbor.id in visited:
                    continue
                if edge.relation_type.startswith("reverse_"):
                    continue

                visited.add(neighbor.id)
                path.append(neighbor.id)
                dfs(neighbor.id, target, path, visited)
                path.pop()
                visited.remove(neighbor.id)

        dfs(source, [source], target, set([source]))
        return paths
