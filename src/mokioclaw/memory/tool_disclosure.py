"""
工具渐进式披露机制

解决面试官问题："如何减少工具过多带来的 Token 消耗"

策略：
- 第一轮：只加载工具名称 + 简短描述（~20 token/工具）
- 第二轮：根据意图加载 3-5 个相关工具的完整 Schema
- 预期节省：50 个工具从 20000 token → 2200 token（节省 89%）

面试官提到的数据：
- 50 个工具 × 400 token = 20000 token（全量加载）
- 渐进式披露：50 × 20 + 3 × 400 = 2200 token
- 节省 89%
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


@dataclass
class ToolMetadata:
    """工具元数据（精简版）"""

    name: str
    description: str
    category: str = "general"
    keywords: list[str] = field(default_factory=list)


@dataclass
class ToolSchema:
    """工具完整 Schema"""

    name: str
    description: str
    parameters: dict[str, Any]
    category: str = "general"


class ToolRegistry:
    """工具注册表

    管理所有可用工具的元数据和完整 Schema
    """

    def __init__(self):
        self._tools: dict[str, ToolMetadata] = {}
        self._schemas: dict[str, ToolSchema] = {}

    def register(self, metadata: ToolMetadata, schema: ToolSchema | None = None) -> None:
        """注册工具

        Args:
            metadata: 工具元数据（必须）
            schema: 完整 Schema（可选，按需加载）
        """
        self._tools[metadata.name] = metadata
        if schema:
            self._schemas[schema.name] = schema

    def get_metadata(self, name: str) -> ToolMetadata | None:
        """获取工具元数据"""
        return self._tools.get(name)

    def get_schema(self, name: str) -> ToolSchema | None:
        """获取工具完整 Schema"""
        return self._schemas.get(name)

    def list_all_metadata(self) -> list[ToolMetadata]:
        """列出所有工具的元数据（精简版）"""
        return list(self._tools.values())

    def list_all_schemas(self) -> list[ToolSchema]:
        """列出所有工具的完整 Schema"""
        return list(self._schemas.values())


class ProgressiveToolDisclosure:
    """工具渐进式披露器

    实现两层披露策略：
    1. 精简列表：只加载名称 + 简短描述（~20 token/工具）
    2. 按需加载：根据意图加载相关工具的完整 Schema

    面试官考察点：
    - 工具过多导致 token 消耗问题
    - 意图识别 + 渐进式披露
    - 关键数据：50 工具全量 20000 token → 渐进式 2200 token（节省 89%）
    """

    # 精简描述的 token 预算（约 20 token）
    BRIEF_DESCRIPTION_MAX_LEN = 80  # 约 20 token（英文 4 char/token）

    # 每轮加载的完整 Schema 数量上限
    FULL_SCHEMA_LIMIT = 5

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or ToolRegistry()
        self._loaded_schemas: set[str] = set()  # 已加载完整 Schema 的工具
        self._last_intent: str | None = None

    def get_brief_tool_list(self) -> str:
        """获取精简工具列表

        返回所有工具的简要描述，用于第一轮意图识别

        Returns:
            格式化的工具列表字符串
        """
        tools = self.registry.list_all_metadata()
        if not tools:
            return "No tools available."

        lines = [f"Available tools ({len(tools)} total):"]
        for tool in tools:
            brief_desc = self._truncate_description(tool.description)
            lines.append(f"- {tool.name}: {brief_desc}")

        return "\n".join(lines)

    def get_full_schemas_for_intent(self, intent: str, user_input: str) -> str:
        """根据意图加载相关工具的完整 Schema

        Args:
            intent: 意图分类（如 "file_operation", "code_execution"）
            user_input: 用户输入（用于关键词匹配）

        Returns:
            格式化的完整 Schema 字符串
        """
        # 1. 根据意图和关键词匹配相关工具
        relevant_tools = self._match_tools_by_intent(intent, user_input)

        # 2. 限制加载数量
        relevant_tools = relevant_tools[: self.FULL_SCHEMA_LIMIT]

        # 3. 加载完整 Schema
        lines = [f"Selected tools for '{intent}':"]
        for tool_name in relevant_tools:
            schema = self.registry.get_schema(tool_name)
            if schema:
                lines.append(f"\n### {schema.name}")
                lines.append(schema.description)
                lines.append(f"```json\n{json.dumps(schema.parameters, ensure_ascii=False, indent=2)}\n```")
                self._loaded_schemas.add(tool_name)

        self._last_intent = intent
        return "\n".join(lines)

    def estimate_token_usage(self, stage: str = "brief") -> dict[str, int]:
        """估算 token 使用量

        Args:
            stage: "brief"（精简列表）或 "full"（完整 Schema）

        Returns:
            token 统计
        """
        if stage == "brief":
            tools = self.registry.list_all_metadata()
            total_chars = sum(
                len(f"- {t.name}: {self._truncate_description(t.description)}\n")
                for t in tools
            )
            return {
                "tool_count": len(tools),
                "total_chars": total_chars,
                "estimated_tokens": max(0, (total_chars + 3) // 4),
            }
        else:
            schemas = self.registry.list_all_schemas()
            loaded = [s for s in schemas if s.name in self._loaded_schemas]
            total_chars = sum(
                len(f"### {s.name}\n{s.description}\n{json.dumps(s.parameters)}")
                for s in loaded
            )
            return {
                "tool_count": len(loaded),
                "total_chars": total_chars,
                "estimated_tokens": max(0, (total_chars + 3) // 4),
            }

    def _truncate_description(self, description: str) -> str:
        """截断描述为精简版"""
        if len(description) <= self.BRIEF_DESCRIPTION_MAX_LEN:
            return description
        return description[: self.BRIEF_DESCRIPTION_MAX_LEN - 3] + "..."

    def _match_tools_by_intent(self, intent: str, user_input: str) -> list[str]:
        """根据意图匹配工具

        优先级：
        1. 按工具分类匹配
        2. 按关键词匹配
        3. 默认返回常用工具
        """
        user_input_lower = user_input.lower()

        # 1. 按意图分类匹配
        intent_category_map = {
            "file_operation": {"FileReadTool", "FileWriteTool", "FileEditTool", "GrepTool"},
            "code_execution": {"BashTool"},
            "web_search": {"WebSearchTool"},
            "planning": {"TodoWriteTool", "TodoUpdateTool"},
            "memory": {"NotepadAppendTool", "NotepadReadTool"},
        }

        if intent in intent_category_map:
            matched = intent_category_map[intent]
            # 过滤出已注册的工具
            return [name for name in matched if name in self.registry._tools]

        # 2. 关键词匹配
        keyword_matches: list[tuple[str, int]] = []
        for tool_name, metadata in self.registry._tools.items():
            score = 0
            # 检查工具名称
            if tool_name.lower().replace("tool", "") in user_input_lower:
                score += 10
            # 检查关键词
            for kw in metadata.keywords:
                if kw in user_input_lower:
                    score += 5
            # 检查描述
            if any(kw in user_input_lower for kw in metadata.description.lower().split()[:10]):
                score += 2

            if score > 0:
                keyword_matches.append((tool_name, score))

        # 按分数排序
        keyword_matches.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in keyword_matches[: self.FULL_SCHEMA_LIMIT]]

    def get_disclosure_stats(self) -> dict[str, Any]:
        """获取披露统计（用于监控）"""
        brief = self.estimate_token_usage("brief")
        full = self.estimate_token_usage("full")
        return {
            "brief": brief,
            "full": full,
            "loaded_schemas": len(self._loaded_schemas),
            "last_intent": self._last_intent,
        }
