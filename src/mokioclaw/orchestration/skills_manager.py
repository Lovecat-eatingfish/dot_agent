"""
Skills 管理器

负责 Skills 的发现、加载、匹配和注入。
对外提供统一接口，供 Session 在初始化时加载可用 Skills。
"""
from __future__ import annotations

from typing import Any, Optional

from mokioclaw.core.log import get_logger
from mokioclaw.plugins.loader import discover_plugin_skills, list_plugin_command_names
from mokioclaw.tools.skill import Skill, discover_skills

logger = get_logger(__name__)


class SkillsManager:
    """Skills 管理器

    管理本地 Skills 和插件 Skills 的发现、加载和匹配。
    """

    def __init__(self, workspace: Optional[Any] = None) -> None:
        self._workspace = workspace
        self._skills: list[Skill] = []
        self._loaded: dict[str, Skill] = {}

    def discover(self, skills_dir: Optional[Any] = None) -> list[Skill]:
        """发现指定目录下的所有 Skills

        Args:
            skills_dir: Skills 目录，None 则使用默认路径

        Returns:
            发现的 Skill 列表
        """
        root = skills_dir or self._workspace
        if root is None:
            return []
        try:
            self._skills = discover_skills(root)
        except Exception as exc:
            logger.debug("skills discover failed: %s", exc)
            self._skills = []
        return list(self._skills)

    def discover_plugin_skills(self) -> list[Skill]:
        """发现已启用插件中的 Skills"""
        try:
            return discover_plugin_skills(self._workspace)
        except Exception as exc:
            logger.debug("plugin skills discover failed: %s", exc)
            return []

    def load_skill(self, name: str) -> Optional[Skill]:
        """按名称加载 Skill

        Args:
            name: Skill 名称

        Returns:
            Skill 对象，不存在则返回 None
        """
        for skill in self._skills:
            if skill.name == name:
                self._loaded[name] = skill
                return skill
        return None

    def get_loaded_skills(self) -> list[Skill]:
        """获取已加载的 Skills"""
        return list(self._loaded.values())

    def match_skills(self, text: str) -> list[Skill]:
        """根据文本匹配相关 Skills

        Args:
            text: 输入文本

        Returns:
            匹配的 Skill 列表
        """
        return [s for s in self._skills if s.matches_keyword(text)]

    def list_plugin_commands(self) -> list[str]:
        """列出已启用插件中的命令名称"""
        try:
            return list_plugin_command_names(self._workspace)
        except Exception as exc:
            logger.debug("list plugin commands failed: %s", exc)
            return []

    def get_all_skills(self) -> list[Skill]:
        """获取所有发现的 Skills（本地 + 插件）"""
        local = list(self._skills)
        plugin = self.discover_plugin_skills()
        return local + plugin
