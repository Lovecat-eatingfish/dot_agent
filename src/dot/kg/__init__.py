"""
dot.kg — Knowledge Graph RAG 服务

独立的知识图谱 RAG 服务，不依赖原有的 dot.rag 模块。
支持实体识别、关系抽取、图存储、图检索。

架构:
  extractor (LLM) → graph_store (NetworkX/Neo4j) → graph_retriever → pipeline → server
"""
from __future__ import annotations

from .extractor import Entity, Relation, EntityExtractor
from .graph_store import KnowledgeGraph, GraphNode, GraphEdge
from .graph_retriever import GraphRetriever
from .pipeline import KGPipeline
from .config import KGSettings

__all__ = [
    "Entity",
    "Relation",
    "EntityExtractor",
    "KnowledgeGraph",
    "GraphNode",
    "GraphEdge",
    "GraphRetriever",
    "KGPipeline",
    "KGSettings",
]
