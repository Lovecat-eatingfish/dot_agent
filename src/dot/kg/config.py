"""
dot.kg.config — KG RAG 配置
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# src/dot/kg/config.py → 项目根（锚定持久化路径，避免受进程 CWD 影响）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@dataclass
class KGSettings:
    """KG RAG 全局配置"""

    # Neo4j 配置（可选，不配置则使用内存图）
    neo4j_uri: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # LLM 配置（留空的字段走 resolve_* 兜底链路，最终回退到 dot.ai 的 .env 配置）
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = ""
    extraction_model: str = ""
    generation_model: str = ""

    # 图检索配置
    max_hops: int = 2  # 最大跳数
    max_entities: int = 50  # 最大返回实体数
    min_confidence: float = 0.5  # 最小置信度

    # 索引配置
    storage_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".dot" / "kg")
    persist_graph: bool = True  # 是否持久化图数据

    # 实体类型白名单
    entity_types: tuple[str, ...] = (
        "PERSON", "ORGANIZATION", "PRODUCT", "LOCATION",
        "EVENT", "CONCEPT", "TECHNOLOGY", "OTHER",
    )

    # 常用关系类型
    relation_types: tuple[str, ...] = (
        "CEO_of", "founded_by", "located_in", "developed_by",
        "part_of", "competes_with", "acquired_by", "married_to",
        "works_at", "created_by", "uses", "depends_on",
    )

    def resolve_openai_key(self) -> str:
        """获取 API Key：显式配置 → OPENAI_API_KEY 环境变量 → dot.ai 的 .env 配置（API_KEY）"""
        if self.openai_api_key:
            return self.openai_api_key
        if os.environ.get("OPENAI_API_KEY"):
            return os.environ["OPENAI_API_KEY"]
        return self._dot_ai_settings().api_key

    def resolve_base_url(self) -> str:
        """获取 base_url：显式配置 → OPENAI_BASE_URL 环境变量 → dot.ai 的 .env 配置（BASE_URL）"""
        if self.openai_base_url:
            return self.openai_base_url
        if os.environ.get("OPENAI_BASE_URL"):
            return os.environ["OPENAI_BASE_URL"]
        return self._dot_ai_settings().base_url

    def resolve_extraction_model(self) -> str:
        """获取抽取模型：显式配置 → dot.ai 的 .env 配置（MODEL）→ gpt-4o-mini"""
        return self.extraction_model or self.openai_model or self._dot_ai_settings().model or "gpt-4o-mini"

    def resolve_generation_model(self) -> str:
        """获取生成模型：留空则复用抽取模型"""
        return self.generation_model or self.resolve_extraction_model()

    @staticmethod
    def _dot_ai_settings():
        """延迟导入 dot.ai 配置（pydantic-settings 会读取项目根 .env）"""
        from dot.ai.config import OpenAISettings

        return OpenAISettings()
