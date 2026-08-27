"""
dot.coding.skills.skill — Skill 定义

SKILL.md YAML frontmatter 解析。
Skills 是提示词，不是工具。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Skill:
    """Skill 定义"""
    name: str
    description: str
    content: str
    path: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> Skill | None:
        """从 SKILL.md 文件解析"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        # 解析 YAML frontmatter
        if not text.startswith("---"):
            return cls(
                name=path.stem,
                description="",
                content=text,
                path=path,
            )

        parts = text.split("---", 2)
        if len(parts) < 3:
            return cls(name=path.stem, description="", content=text, path=path)

        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            meta = {}

        content = parts[2].strip()
        return cls(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            content=content,
            path=path,
        )

    def to_prompt_section(self) -> str:
        """转换为 system prompt 注入格式"""
        return f'<skill name="{self.name}">\n<description>{self.description}</description>\n<content>\n{self.content}\n</content>\n</skill>'
