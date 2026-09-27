"""
dot.kg.graph_store — 知识图谱存储

支持两种存储后端:
1. NetworkX (默认) - 内存图，适合小规模数据
2. Neo4j (可选) - 图数据库，适合大规模生产环境

设计原则:
- 实体和关系都是 immutable (frozen=True)
- 支持持久化到磁盘 (JSON/GraphML)
- 支持导出为可视化格式 (JSON, Dict)
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["KnowledgeGraph", "GraphNode", "GraphEdge"]

# ============================================================
# 数据结构
# ============================================================


@dataclass(frozen=True, slots=True)
class GraphNode:
    """图节点"""
    id: str  # 实体名作为 ID
    type: str
    description: str = ""
    properties: dict = field(default_factory=dict)  # 额外属性

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "properties": self.properties,
        }

    @classmethod
    def from_entity(cls, entity: Any) -> GraphNode:
        """从 Entity 创建节点"""
        return cls(
            id=entity.name,
            type=entity.type,
            description=entity.description,
            properties={"source_chunk": getattr(entity, "source_chunk", "")[:500]},
        )


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """图边"""
    source: str
    target: str
    relation_type: str
    weight: float = 1.0
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type,
            "weight": self.weight,
            "properties": self.properties,
        }

    @classmethod
    def from_relation(cls, relation: Any) -> GraphEdge:
        """从 Relation 创建边"""
        return cls(
            source=relation.source,
            target=relation.target,
            relation_type=relation.relation_type,
            weight=relation.confidence,
            properties={"source_chunk": getattr(relation, "source_chunk", "")[:500]},
        )


# ============================================================
# 知识图谱存储
# ============================================================


class KnowledgeGraph:
    """知识图谱存储

    使用邻接表存储，支持:
    - 实体 CRUD
    - 关系 CRUD
    - BFS/DFS 遍历
    - 持久化/加载
    - 导出为 NetworkX 图
    """

    def __init__(self, name: str = "kg"):
        self.name = name
        self._nodes: dict[str, GraphNode] = {}  # node_id -> GraphNode
        self._edges: list[GraphEdge] = []  # 所有边
        self._adj: dict[str, dict[str, list[GraphEdge]]] = defaultdict(lambda: defaultdict(list))
        # _adj[source][target] -> [edges]
        self._type_index: dict[str, set[str]] = defaultdict(set)  # type -> node_ids

    # ============================================================
    # CRUD 操作
    # ============================================================

    def add_node(self, node: GraphNode) -> None:
        """添加节点"""
        self._nodes[node.id] = node
        self._type_index[node.type].add(node.id)

    def add_edge(self, edge: GraphEdge) -> None:
        """添加边"""
        # 确保节点存在
        if edge.source not in self._nodes:
            self.add_node(GraphNode(id=edge.source, type="UNKNOWN"))
        if edge.target not in self._nodes:
            self.add_node(GraphNode(id=edge.target, type="UNKNOWN"))

        self._edges.append(edge)
        self._adj[edge.source][edge.target].append(edge)

        # 无向图：同时添加反向边
        reverse = GraphEdge(
            source=edge.target,
            target=edge.source,
            relation_type=f"reverse_{edge.relation_type}",
            weight=edge.weight,
            properties=edge.properties,
        )
        self._adj[edge.target][edge.source].append(reverse)

    def get_node(self, node_id: str) -> GraphNode | None:
        """获取节点"""
        return self._nodes.get(node_id)

    def get_neighbors(self, node_id: str, edge_type: str | None = None, include_reverse: bool = True) -> list[tuple[GraphNode, GraphEdge]]:
        """获取邻居节点

        Args:
            node_id: 节点 ID
            edge_type: 过滤特定类型的边
            include_reverse: 是否包含反向边
        """
        if node_id not in self._adj:
            return []

        neighbors = []
        for target, edges in self._adj[node_id].items():
            for edge in edges:
                if not include_reverse and edge.relation_type.startswith("reverse_"):
                    continue
                if edge_type is None or edge.relation_type == edge_type:
                    if target in self._nodes:
                        neighbors.append((self._nodes[target], edge))
        return neighbors

    def has_node(self, node_id: str) -> bool:
        """检查节点是否存在"""
        return node_id in self._nodes

    def has_edge(self, source: str, target: str, relation_type: str | None = None) -> bool:
        """检查边是否存在"""
        if source not in self._adj:
            return False
        if target not in self._adj[source]:
            return False
        if relation_type is None:
            return len(self._adj[source][target]) > 0
        return any(e.relation_type == relation_type for e in self._adj[source][target])

    # ============================================================
    # 批量操作
    # ============================================================

    def add_entities(self, entities: list[Any]) -> None:
        """批量添加实体"""
        for e in entities:
            if hasattr(e, "name") and hasattr(e, "type"):
                self.add_node(GraphNode.from_entity(e))

    def add_relations(self, relations: list[Any]) -> None:
        """批量添加关系"""
        for r in relations:
            if hasattr(r, "source") and hasattr(r, "target") and hasattr(r, "relation_type"):
                self.add_edge(GraphEdge.from_relation(r))

    def add_extraction_result(self, entities: list[Any], relations: list[Any]) -> None:
        """添加抽取结果"""
        self.add_entities(entities)
        self.add_relations(relations)

    # ============================================================
    # 查询操作
    # ============================================================

    def search_nodes(self, query: str, limit: int = 10) -> list[GraphNode]:
        """搜索节点（按名称模糊匹配）"""
        query_lower = query.lower()
        matched = []
        for node in self._nodes.values():
            if query_lower in node.id.lower():
                matched.append(node)
                if len(matched) >= limit:
                    break
        return matched

    def get_nodes_by_type(self, node_type: str) -> list[GraphNode]:
        """按类型获取节点"""
        node_ids = self._type_index.get(node_type, set())
        return [self._nodes[nid] for nid in node_ids if nid in self._nodes]

    def bfs_expand(
        self,
        start_nodes: list[str],
        max_hops: int = 2,
        edge_types: list[str] | None = None,
    ) -> dict[str, list[tuple[GraphNode, GraphEdge, int]]]:
        """BFS 扩展获取子图

        Returns:
            {node_id: [(neighbor_node, edge, hop), ...]}
        """
        result: dict[str, list[tuple[GraphNode, GraphEdge, int]]] = defaultdict(list)
        visited = set(start_nodes)
        queue: list[tuple[str, int]] = [(n, 0) for n in start_nodes]

        while queue:
            current, depth = queue.pop(0)
            if depth >= max_hops:
                continue

            # 包含反向边（因为是无向图）
            for neighbor, edge in self.get_neighbors(current, include_reverse=True):
                if neighbor.id in visited:
                    continue
                if edge_types and not any(et in edge.relation_type for et in edge_types):
                    continue

                visited.add(neighbor.id)
                result[neighbor.id].append((neighbor, edge, depth + 1))
                queue.append((neighbor.id, depth + 1))

        return result

    def get_subgraph(self, center_nodes: list[str], max_hops: int = 2) -> KnowledgeGraph:
        """获取子图"""
        subgraph = KnowledgeGraph(name=f"{self.name}_subgraph")

        # BFS 扩展
        expanded = self.bfs_expand(center_nodes, max_hops)

        # 添加中心节点
        for node_id in center_nodes:
            if node_id in self._nodes:
                subgraph.add_node(self._nodes[node_id])

        # 添加扩展节点和边
        for node_id, neighbors in expanded.items():
            if node_id in self._nodes:
                subgraph.add_node(self._nodes[node_id])
            for neighbor, edge, _ in neighbors:
                subgraph.add_node(neighbor)
                # 去掉反向边
                if not edge.relation_type.startswith("reverse_"):
                    subgraph.add_edge(edge)

        # 添加边（从中心节点到直接邻居）
        for node_id in center_nodes:
            for neighbor, edge in self.get_neighbors(node_id):
                if edge.relation_type.startswith("reverse_"):
                    continue
                subgraph.add_node(neighbor)
                subgraph.add_edge(edge)

        return subgraph

    # ============================================================
    # 统计
    # ============================================================

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len([e for e in self._edges if not e.relation_type.startswith("reverse_")])

    def stats(self) -> dict[str, Any]:
        """获取统计信息"""
        type_counts = {t: len(ids) for t, ids in self._type_index.items()}
        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "type_distribution": type_counts,
        }

    # ============================================================
    # 持久化
    # ============================================================

    def to_dict(self) -> dict[str, Any]:
        """导出为 dict"""
        return {
            "name": self.name,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges if not e.relation_type.startswith("reverse_")],
        }

    def to_json(self, path: Path | str) -> None:
        """持久化到 JSON 文件"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Graph persisted to {path}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeGraph:
        """从 dict 加载"""
        kg = cls(name=data.get("name", "kg"))

        for node_data in data.get("nodes", []):
            kg.add_node(GraphNode(**node_data))

        for edge_data in data.get("edges", []):
            kg.add_edge(GraphEdge(**edge_data))

        return kg

    @classmethod
    def from_json(cls, path: Path | str) -> KnowledgeGraph:
        """从 JSON 文件加载"""
        path = Path(path)
        if not path.exists():
            logger.warning(f"Graph file not found: {path}")
            return cls()

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    # ============================================================
    # NetworkX 兼容
    # ============================================================

    def to_networkx(self) -> Any:
        """导出为 NetworkX 图"""
        try:
            import networkx as nx

            G = nx.DiGraph()
            for node in self._nodes.values():
                G.add_node(node.id, type=node.type, description=node.description)

            for edge in self._edges:
                if not edge.relation_type.startswith("reverse_"):
                    G.add_edge(edge.source, edge.target, relation=edge.relation_type, weight=edge.weight)

            return G
        except ImportError:
            logger.warning("networkx not installed, cannot convert")
            return None

    # ============================================================
    # 调试
    # ============================================================

    def __repr__(self) -> str:
        return f"KnowledgeGraph(name={self.name}, nodes={self.node_count}, edges={self.edge_count})"

    def pprint(self, max_nodes: int = 20) -> None:
        """打印图结构"""
        print(f"KnowledgeGraph: {self.name}")
        print(f"  Nodes: {self.node_count}, Edges: {self.edge_count}")
        print("  Node types:")
        for t, ids in self._type_index.items():
            print(f"    {t}: {len(ids)}")
        print("  Sample nodes:")
        for i, node in enumerate(list(self._nodes.values())[:max_nodes]):
            print(f"    [{node.type}] {node.id}")
