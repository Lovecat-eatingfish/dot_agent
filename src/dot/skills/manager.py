"""
Skills 管理器（dot 版，裁剪掉 plugins 依赖）

负责 Skills 的发现、加载、匹配：
- 发现路径：~/.mokioclaw/skills（全局）+ <workspace>/.mokioclaw/skills（项目级）
- 项目级同名覆盖全局
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.log import get_logger
from .skill import Skill, discover_skills

logger = get_logger(__name__)


class SkillsManager:
    """Skills 管理器"""

    def __init__(self, workspace: Optional[Any] = None) -> None:
        self._workspace = workspace
        self._skills: list[Skill] = []
        self._loaded: dict[str, Skill] = {}

    def discover(self, skills_dir: Optional[Any] = None) -> list[Skill]:
        """发现指定目录下的所有 Skills

        Args:
            skills_dir: Skills 目录，None 则扫描全局 + 项目级默认路径
        """
        if skills_dir is not None:
            root = Path(skills_dir)
            try:
                self._skills = discover_skills(root)
            except Exception as exc:
                logger.debug("skills discover failed: %s", exc)
                self._skills = []
            return list(self._skills)

        # 全局 + 项目级，项目级同名覆盖全局
        skills: list[Skill] = []
        seen: set[str] = set()
        for directory in self._default_skill_dirs():
            for skill in discover_skills(directory):
                if skill.name not in seen:
                    skills.append(skill)
                    seen.add(skill.name)
        self._skills = skills
        return list(self._skills)

    def _default_skill_dirs(self) -> list[Path]:
        dirs = [Path.home() / ".dot" / "skills"]
        if self._workspace is not None:
            ws = Path(self._workspace)
            dirs.append(ws / ".dot" / "skills")
        return dirs

    def load_skill(self, name: str) -> Optional[Skill]:
        """按名称加载 Skill（大小写不敏感兜底）"""
        for skill in self._skills:
            if skill.name == name:
                self._loaded[name] = skill
                return skill
        for skill in self._skills:
            if skill.name.lower() == name.lower():
                self._loaded[skill.name] = skill
                return skill
        return None

    def get_loaded_skills(self) -> list[Skill]:
        """获取已加载的 Skills"""
        return list(self._loaded.values())

    def match_skills(self, text: str) -> list[Skill]:
        """根据文本匹配相关 Skills"""
        return [s for s in self._skills if s.matches_keyword(text)]

    def get_all_skills(self) -> list[Skill]:
        """获取所有发现的 Skills"""
        if not self._skills:
            self.discover()
        return list(self._skills)
