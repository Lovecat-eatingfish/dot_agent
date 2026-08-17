"""
提示词构建器

动静分离设计（对齐 Claude Code prompt caching）：
- 静态层：agent_prompt.py 模板（角色 / 规则 / 工具用法）— 可缓存前缀
- 分界线：__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
- 动态层：cwd / OS / 日期 / CLAUDE.md 自定义指令 / MEMORY.md 索引 / skills 目录 / thinking mode
- 运行时层：task / plan / memory 由各节点 HumanMessage 注入
"""
from __future__ import annotations

import platform
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mokioclaw.config.loader import UserConfig, load_user_config
from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import project_memory_dir
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

SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

_CUSTOM_SECTION_HEADER = "\n## User Custom Instructions\n"


class PromptBuilder:
    """提示词构建器：静态模板 + 动态环境块"""

    def __init__(
        self,
        workspace: Path | None = None,
        user_config: UserConfig | None = None,
        *,
        thinking_instruction: str = "",
        runtime: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._config = user_config if user_config is not None else self._load(workspace)
        self._thinking_instruction = thinking_instruction.strip()
        self._runtime = runtime

    @staticmethod
    def _load(workspace: Path | None) -> UserConfig:
        try:
            return load_user_config(workspace)
        except Exception as exc:
            logger.debug("PromptBuilder config load failed, using defaults: %s", exc)
            return UserConfig()

    @property
    def user_config(self) -> UserConfig:
        return self._config

    def with_thinking(self, instruction: str) -> PromptBuilder:
        """返回带思考模式指令的新 builder（不污染全局单例）"""
        return PromptBuilder(
            workspace=self._workspace,
            user_config=self._config,
            thinking_instruction=instruction,
        )

    def build_parts(self, agent: str, *, base_prompt: str | None = None) -> tuple[str, str]:
        """返回 (static_prompt, dynamic_block)"""
        templates = {
            "planner": PLANNER_PROMPT,
            "search_agent": SEARCH_AGENT_PROMPT,
            "code_agent": CODE_AGENT_PROMPT,
            "verifier": VERIFIER_PROMPT,
            "intent_router": INTENT_ROUTER_PROMPT,
            "chat_responder": CHAT_RESPONDER_PROMPT,
            "context_compressor": CONTEXT_COMPRESSION_PROMPT,
        }
        static = base_prompt if base_prompt is not None else templates.get(agent, "")
        if not static:
            logger.warning("PromptBuilder: unknown agent '%s'", agent)
        return static, self._build_dynamic_block()

    def build(self, agent: str, *, base_prompt: str | None = None) -> str:
        """构建完整系统提示词（静态 + 分界线 + 动态）"""
        static, dynamic = self.build_parts(agent, base_prompt=base_prompt)
        # 未知 agent 且未提供 base_prompt → 空字符串（保持旧契约）
        if not static and base_prompt is None:
            return ""
        if not static:
            return dynamic
        if not dynamic.strip():
            return static
        return f"{static.rstrip()}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n{dynamic}"

    def build_system_message(self, agent: str, *, base_prompt: str | None = None) -> str:
        return self.build(agent, base_prompt=base_prompt)

    def with_workspace(self, workspace: Path) -> PromptBuilder:
        return PromptBuilder(
            workspace=workspace,
            user_config=self._config,
            thinking_instruction=self._thinking_instruction,
        )

    def _build_dynamic_block(self) -> str:
        sections: list[str] = []

        # 环境信息
        cwd = str(self._workspace.resolve()) if self._workspace else "."
        agent_mode = ""
        if self._runtime is not None:
            agent_mode = str(getattr(self._runtime, "agent_mode", "") or "")
        env_lines = [
            "## Environment",
            f"Current working directory: {cwd}",
            f"OS: {platform.system()} {platform.release()}",
            f"Date: {datetime.now().strftime('%Y-%m-%d')}",
        ]
        if agent_mode:
            env_lines.append(f"Agent mode: {agent_mode}")
        sections.append("\n".join(env_lines))

        # MCP 目录（渐进披露）
        mcp_catalog = self._load_mcp_catalog()
        if mcp_catalog:
            sections.append("## MCP Catalog\n" + mcp_catalog)

        # 用户自定义指令（CLAUDE.md / config.md body）
        instructions = (self._config.custom_instructions or "").strip()
        if instructions:
            sections.append(f"{_CUSTOM_SECTION_HEADER}{instructions}")

        # MEMORY.md 索引（主题正文按需 FileRead）
        memory_index = self._load_memory_index()
        if memory_index:
            sections.append("## Memory Index (MEMORY.md)\n" + memory_index)

        # Skills 目录（仅元数据）
        skill_catalog = self._load_skill_catalog()
        if skill_catalog:
            sections.append("## Skills\n" + skill_catalog)

        # Thinking mode（builder 显式参数优先，否则读 runtime）
        thinking = self._thinking_instruction
        if not thinking and self._runtime is not None:
            thinking = str(getattr(self._runtime, "thinking_instruction", "") or "").strip()
        if thinking:
            sections.append("## Thinking Mode\n" + thinking)

        # SessionStart hook 注入的上下文（对齐 Claude Code stdout→context）
        if self._runtime is not None:
            hook_ctx = str(getattr(self._runtime, "session_context_injection", "") or "").strip()
            if hook_ctx:
                sections.append("## Session Context (from hooks)\n" + hook_ctx)
            deferred = str(getattr(self._runtime, "deferred_tool_catalog", "") or "").strip()
            if deferred:
                sections.append("## Deferred Tools\n" + deferred)
            cwd = getattr(self._runtime, "cwd", None)
            if cwd is not None:
                sections.append(f"## Working Directory Override\nCurrent tool cwd: {cwd}")

        return "\n\n".join(sections)

    def _load_memory_index(self) -> str:
        if self._workspace is None:
            return ""
        try:
            from mokioclaw.memory.topic_store import TopicStore
            return TopicStore(self._workspace).load_index().strip()
        except Exception as exc:
            logger.debug("memory index load failed: %s", exc)
            return ""

    def _load_mcp_catalog(self) -> str:
        try:
            from mokioclaw.mcp.bridge import get_mcp_bridge
            from mokioclaw.mcp.disclosure import build_mcp_catalog_text, should_defer_mcp_schemas

            if self._workspace is None:
                return ""
            bridge = get_mcp_bridge(self._workspace)
            if not should_defer_mcp_schemas(bridge):
                # 工具不多时不占动态区
                servers = bridge.list_servers()
                if not servers:
                    return ""
                return "MCP servers: " + ", ".join(servers)
            return build_mcp_catalog_text(bridge)
        except Exception as exc:
            logger.debug("mcp catalog load failed: %s", exc)
            return ""

    def _load_skill_catalog(self) -> str:
        try:
            from mokioclaw.tools.skill import build_skill_catalog, discover_skills
            from pathlib import Path

            skills = []
            seen: set[str] = set()
            dirs = [Path.home() / ".mokioclaw" / "skills"]
            if self._workspace is not None:
                dirs.append(self._workspace / ".mokioclaw" / "skills")
            for d in dirs:
                for skill in discover_skills(d):
                    if skill.name not in seen:
                        skills.append(skill)
                        seen.add(skill.name)
            # 已启用插件提供的 skills（命名空间 plugin:<name>，避免与本地同名冲突）
            try:
                from mokioclaw.plugins.loader import discover_plugin_skills

                for skill in discover_plugin_skills(self._workspace):
                    if skill.name not in seen:
                        skills.append(skill)
                        seen.add(skill.name)
            except Exception as exc:
                logger.debug("plugin skill catalog load failed: %s", exc)
            return build_skill_catalog(skills)
        except Exception as exc:
            logger.debug("skill catalog load failed: %s", exc)
            return ""

    def __repr__(self) -> str:
        sources = ", ".join(self._config.config_sources) or "defaults"
        return f"PromptBuilder(sources=[{sources}], custom_instructions={len(self._config.custom_instructions)} chars)"


_builder: PromptBuilder | None = None


def get_prompt_builder(workspace: Path | None = None, runtime: Any | None = None) -> PromptBuilder:
    """获取 PromptBuilder

    若传入 runtime，每次返回绑定该 runtime 的实例（读取 thinking_instruction）。
    否则使用进程级单例。
    """
    global _builder
    if runtime is not None:
        ws = workspace or getattr(runtime, "workspace", None)
        return PromptBuilder(workspace=ws, runtime=runtime)
    if _builder is None:
        _builder = PromptBuilder(workspace=workspace)
    elif workspace is not None and _builder._workspace != workspace:
        # 重建并重新加载该 workspace 的用户配置（复用旧 config 会注入过期指令）
        _builder = PromptBuilder(workspace=workspace)
    return _builder


def reset_prompt_builder() -> None:
    global _builder
    _builder = None
