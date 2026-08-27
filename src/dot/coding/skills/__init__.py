"""
dot.coding.skills — Skills 系统

Skills 是提示词，不是工具。
  - SKILL.md YAML frontmatter 定义 skill 元数据
  - skill 索引（name + description）注入 system prompt 的 <available_skills> 块
  - /skill:name 展开为 XML 注入消息
"""
from __future__ import annotations

from .skill import Skill
from .manager import SkillManager

__all__ = [
    "Skill",
    "SkillManager",
]
