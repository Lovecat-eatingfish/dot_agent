"""
dot.coding.skills.manager — SkillManager 目录扫描与索引构建

扫描 SKILL.md 文件，构建 skill 索引用于 system prompt 注入。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .skill import Skill

logger = logging.getLogger(__name__)


class SkillManager:
    """Skill 管理器"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def scan_directory(self, directory: Path) -> int:
        """扫描目录中的 SKILL.md 文件"""
        count = 0
        for path in directory.rglob("SKILL.md"):
            skill = Skill.from_file(path)
            if skill:
                self._skills[skill.name] = skill
                count += 1
        logger.info("[skills] Scanned %d skills from %s", count, directory)
        return count

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_skill_index(self) -> str:
        """获取 skill 索引文本（注入 system prompt）"""
        if not self._skills:
            return ""
        lines = ["<available_skills>"]
        for skill in self._skills.values():
            lines.append(f'  <skill name="{skill.name}" description="{skill.description}" />')
        lines.append("</available_skills>")
        return "\n".join(lines)

    def expand_skill(self, name: str) -> str:
        """展开 skill 为完整内容（/skill:name 调用）"""
        skill = self._skills.get(name)
        if skill is None:
            return f"Skill '{name}' not found"
        return skill.to_prompt_section()
