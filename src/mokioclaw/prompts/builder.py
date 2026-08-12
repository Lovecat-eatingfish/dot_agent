"""
提示词构建器

动静分离设计：
- 静态层：agent_prompt.py 中的模板字符串（角色定义、规则、输出格式）
- 动态层：用户自定义指令（来自 ~/.mokioclaw/CLAUDE.md 和 .mokioclaw/config.md）
- 运行时层：任务数据（task、plan、memory）— 由各节点在调用时注入

PromptBuilder 负责合并静态模板 + 动态配置，输出完整的 SystemMessage 内容。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mokioclaw.config.loader import UserConfig, load_user_config
from mokioclaw.core.log import get_logger
from mokioclaw.prompts.agent_prompt import (
    CHAT_RESPONDER_PROMPT,
    CODE_AGENT_PROMPT,
    INTENT_ROUTER_PROMPT,
    PLANNER_PROMPT,
    SEARCH_AGENT_PROMPT,
    VERIFIER_PROMPT,
)
from mokioclaw.prompts.context_manager_prompt import CONTEXT_COMPRESSION_PROMPT

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# 自定义指令的分隔标记
_CUSTOM_SECTION_HEADER = "\n\n## User Custom Instructions\n"
_CUSTOM_SECTION_FOOTER = "\n"


class PromptBuilder:
    """提示词构建器

    合并静态模板与动态用户配置，生成完整的系统提示词。

    使用方式：
        builder = PromptBuilder(workspace=Path("."))
        system_content = builder.build("code_agent")
        messages = [SystemMessage(content=system_content), HumanMessage(content=task)]
    """

    def __init__(self, workspace: Path | None = None, user_config: UserConfig | None = None) -> None:
        """初始化

        Args:
            workspace: 工作区路径，用于查找项目级配置
            user_config: 预加载的用户配置，为 None 时自动加载
        """
        self._workspace = workspace
        self._config = user_config if user_config is not None else self._load(workspace)
        self._custom_block = self._build_custom_block()

    @staticmethod
    def _load(workspace: Path | None) -> UserConfig:
        """加载用户配置（带异常保护）"""
        try:
            return load_user_config(workspace)
        except Exception as exc:
            logger.debug("PromptBuilder config load failed, using defaults: %s", exc)
            return UserConfig()

    def _build_custom_block(self) -> str:
        """构建用户自定义指令块

        如果用户配置中有 custom_instructions，包装成标准格式；
        否则返回空字符串。
        """
        instructions = (self._config.custom_instructions or "").strip()
        if not instructions:
            return ""
        return f"{_CUSTOM_SECTION_HEADER}{instructions}{_CUSTOM_SECTION_FOOTER}"

    @property
    def user_config(self) -> UserConfig:
        """当前使用的用户配置"""
        return self._config

    def build(self, agent: str, *, base_prompt: str | None = None) -> str:
        """构建指定 agent 的完整系统提示词

        Args:
            agent: agent 名称，支持：
                - "planner", "search_agent", "code_agent", "verifier"
                - "intent_router", "chat_responder", "context_compressor"
            base_prompt: 可选，直接指定基础模板（覆盖 agent 参数）

        Returns:
            完整的系统提示词字符串
        """
        templates = {
            "planner": PLANNER_PROMPT,
            "search_agent": SEARCH_AGENT_PROMPT,
            "code_agent": CODE_AGENT_PROMPT,
            "verifier": VERIFIER_PROMPT,
            "intent_router": INTENT_ROUTER_PROMPT,
            "chat_responder": CHAT_RESPONDER_PROMPT,
            "context_compressor": CONTEXT_COMPRESSION_PROMPT,
        }

        template = base_prompt if base_prompt is not None else templates.get(agent, "")
        if not template:
            logger.warning("PromptBuilder: unknown agent '%s', returning base template", agent)
            return template

        if not self._custom_block:
            return template

        # 静态模板 + 动态自定义指令
        return template + self._custom_block

    def build_system_message(self, agent: str, *, base_prompt: str | None = None) -> str:
        """构建 SystemMessage 内容（别名，与 build 等价，语义更清晰）"""
        return self.build(agent, base_prompt=base_prompt)

    def with_workspace(self, workspace: Path) -> PromptBuilder:
        """创建指定 workspace 的新实例（保持当前 user_config）"""
        return PromptBuilder(workspace=workspace, user_config=self._config)

    def __repr__(self) -> str:
        sources = ", ".join(self._config.config_sources) or "defaults"
        return f"PromptBuilder(sources=[{sources}], custom_instructions={len(self._config.custom_instructions)} chars)"


# 模块级单例缓存（每个进程只需加载一次配置）
_builder: PromptBuilder | None = None


def get_prompt_builder(workspace: Path | None = None) -> PromptBuilder:
    """获取全局 PromptBuilder 单例

    首次调用时加载配置并缓存，后续调用返回缓存实例。
    如需强制刷新，先调用 reset_prompt_builder()。
    """
    global _builder
    if _builder is None:
        _builder = PromptBuilder(workspace=workspace)
    return _builder


def reset_prompt_builder() -> None:
    """重置全局单例，下次 get_prompt_builder() 时重新加载配置"""
    global _builder
    _builder = None
