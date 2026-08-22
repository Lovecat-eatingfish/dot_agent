"""
dot.skills — Skill 渐进披露

  - skill:   Skill 定义与磁盘发现（SKILL.md / skill.yaml）
  - manager: Skills 管理器（全局 + 项目级扫描）
  - host:    SkillHost（目录注入 + invoke_skill 延迟加载）
"""
from __future__ import annotations

from .host import SkillHost
from .manager import SkillsManager
from .skill import Skill, build_skill_catalog, discover_skills, load_skill_markdown

__all__ = [
    "Skill",
    "discover_skills",
    "load_skill_markdown",
    "build_skill_catalog",
    "SkillsManager",
    "SkillHost",
]
