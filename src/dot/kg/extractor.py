"""
dot.kg.extractor — 实体识别与关系抽取

使用 LLM 从文本中抽取实体和关系，构建知识图谱。
支持同步/异步两种模式，支持批量处理。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["Entity", "Relation", "EntityExtractor"]


# ============================================================
# 数据结构
# ============================================================


@dataclass(frozen=True, slots=True)
class Entity:
    """实体"""
    name: str
    type: str  # PERSON, ORGANIZATION, PRODUCT, etc.
    description: str = ""
    source_chunk: str = ""  # 来源文本片段

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "source_chunk": self.source_chunk,
        }


@dataclass(frozen=True, slots=True)
class Relation:
    """关系"""
    source: str  # 源实体名
    target: str  # 目标实体名
    relation_type: str  # CEO_of, founded_by, etc.
    confidence: float = 1.0
    source_chunk: str = ""  # 来源文本片段

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type,
            "confidence": self.confidence,
            "source_chunk": self.source_chunk,
        }


@dataclass
class ExtractionResult:
    """抽取结果"""
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    raw_response: str = ""

    def merge(self, other: ExtractionResult) -> ExtractionResult:
        """合并两个抽取结果"""
        # 去重
        entity_names = {e.name for e in self.entities}
        merged_entities = list(self.entities)
        for e in other.entities:
            if e.name not in entity_names:
                merged_entities.append(e)
                entity_names.add(e.name)

        relation_keys = {(r.source, r.target, r.relation_type) for r in self.relations}
        merged_relations = list(self.relations)
        for r in other.relations:
            key = (r.source, r.target, r.relation_type)
            if key not in relation_keys:
                merged_relations.append(r)
                relation_keys.add(key)

        return ExtractionResult(
            entities=merged_entities,
            relations=merged_relations,
        )


# ============================================================
# 实体抽取器
# ============================================================


class EntityExtractor:
    """基于 LLM 的实体和关系抽取器"""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
    ):
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"
        self.model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        """懒加载 OpenAI 客户端"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
            except ImportError:
                logger.warning("openai package not installed, using mock mode")
                self._client = None
        return self._client

    def _build_prompt(self, text: str, entity_types: tuple[str, ...], relation_types: tuple[str, ...]) -> str:
        """构建抽取提示词"""
        entity_types_str = ", ".join(entity_types)
        relation_types_str = ", ".join(relation_types)

        return f"""你是一个实体和关系抽取专家。请从以下文本中抽取所有实体和关系。

文本:
---
{text}
---

要求:
1. 实体类型限于: {entity_types_str}
2. 关系类型限于: {relation_types_str}
3. 只抽取明确存在的实体和关系，不要推测
4. 实体名尽量简洁，不超过50字
5. 关系要是动词或介词短语，如 "CEO_of", "located_in"

返回 JSON 格式:
{{
  "entities": [
    {{"name": "实体名", "type": "类型", "description": "简短描述"}},
    ...
  ],
  "relations": [
    {{"source": "实体A", "target": "实体B", "relation_type": "关系类型"}},
    ...
  ]
}}

只返回 JSON，不要其他内容。"""

    def _parse_response(self, text: str) -> ExtractionResult:
        """解析 LLM 响应"""
        # 提取 JSON
        try:
            # 尝试直接解析
            data = json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取 markdown 代码块
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse extraction response: {text[:200]}")
                    return ExtractionResult(raw_response=text)
            else:
                # 尝试提取 {...} 片段
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        data = json.loads(text[start:end])
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse extraction response: {text[:200]}")
                        return ExtractionResult(raw_response=text)
                else:
                    return ExtractionResult(raw_response=text)

        entities = []
        for e in data.get("entities", []):
            if e.get("name") and e.get("type"):
                entities.append(Entity(
                    name=e["name"].strip(),
                    type=e["type"].upper(),
                    description=e.get("description", ""),
                ))

        relations = []
        for r in data.get("relations", []):
            if r.get("source") and r.get("target") and r.get("relation_type"):
                relations.append(Relation(
                    source=r["source"].strip(),
                    target=r["target"].strip(),
                    relation_type=r["relation_type"].strip(),
                    confidence=r.get("confidence", 1.0),
                ))

        return ExtractionResult(
            entities=entities,
            relations=relations,
            raw_response=text,
        )

    async def extract(self, text: str, source_chunk: str = "") -> ExtractionResult:
        """异步抽取单个文本的实体和关系"""
        client = self._get_client()
        if client is None:
            # Mock 模式
            return self._mock_extract(text, source_chunk)

        from .config import KGSettings

        settings = KGSettings()
        prompt = self._build_prompt(text, settings.entity_types, settings.relation_types)

        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个实体和关系抽取专家。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=2048,
            )

            content = response.choices[0].message.content or ""
            result = self._parse_response(content)

            # 补充 source_chunk
            if source_chunk:
                for e in result.entities:
                    object.__setattr__(e, "source_chunk", source_chunk[:500])
                for r in result.relations:
                    object.__setattr__(r, "source_chunk", source_chunk[:500])

            return result

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return ExtractionResult(raw_response=str(e))

    def _mock_extract(self, text: str, source_chunk: str = "") -> ExtractionResult:
        """Mock 抽取（测试用）"""
        # 简单的规则匹配作为 mock
        entities = []
        relations = []

        # 匹配 "X is Y" 或 "X 是 Y" 模式
        patterns = [
            r"([A-Z][a-zA-Z\s]+?)\s+(?:is|was|are|were|是|为)\s+([A-Z][a-zA-Z\s]+)",
            r"([A-Z][a-zA-Z\s]+?)\s+(?:CEO|创始人|总裁)\s+(?:of|of\s+)?([A-Z][a-zA-Z\s]+)",
        ]

        for pattern in patterns:
            matches = re.finditer(pattern, text)
            for match in matches:
                if match.group(1).strip() != match.group(2).strip():
                    entities.append(Entity(
                        name=match.group(1).strip(),
                        type="ENTITY",
                        source_chunk=source_chunk[:500],
                    ))
                    entities.append(Entity(
                        name=match.group(2).strip(),
                        type="ENTITY",
                        source_chunk=source_chunk[:500],
                    ))

        return ExtractionResult(
            entities=entities,
            relations=relations,
            raw_response="mock",
        )

    async def extract_batch(self, texts: list[str]) -> ExtractionResult:
        """批量抽取多个文本"""
        import asyncio

        results = await asyncio.gather(*[self.extract(t, t[:200]) for t in texts])

        merged = ExtractionResult()
        for r in results:
            merged = merged.merge(r)

        return merged
