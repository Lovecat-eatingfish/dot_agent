"""
Skill 定义与加载

Skill 是一组预配置的工具 + 提示词 + 触发条件，
以 YAML 文件形式存放在 .mokioclaw/skills/ 目录下。

Skill 格式（YAML）：
    name: skill-name
    description: What this skill does
    tools:
      - ToolName1
      - ToolName2
    prompts:
      - AGENT_PROMPT
    triggers:
      - intent: research
      - keyword: search

用法：
    registry = ToolRegistry()
    registry.load_skill(Path(".mokioclaw/skills/web-research/skill.yaml"))
"""
from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any


class Skill:
    """Skill 定义

    属性：
        name: Skill 唯一名称
        description: 功能描述
        tools: 包含的工具名列表
        prompts: 关联的提示词名称列表
        triggers: 触发条件列表
        path: Skill 文件所在目录
    """

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self.name: str = str(data.get("name", ""))
        self.description: str = str(data.get("description", ""))
        self.tools: list[str] = list(data.get("tools", []) or [])
        self.prompts: list[str] = list(data.get("prompts", []) or [])
        self.triggers: list[dict[str, str]] = list(data.get("triggers", []) or [])
        self.path = path
        self._raw = data

    def matches_intent(self, intent: str) -> bool:
        """检查是否匹配指定意图"""
        for trigger in self.triggers:
            if trigger.get("intent") == intent:
                return True
        return False

    def matches_keyword(self, text: str) -> bool:
        """检查文本是否包含 Skill 关联的关键词"""
        text_lower = text.lower()
        for trigger in self.triggers:
            keyword = trigger.get("keyword", "")
            if keyword and keyword.lower() in text_lower:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "prompts": self.prompts,
            "triggers": self.triggers,
        }


def load_skill(path: Path) -> Skill | None:
    """从 YAML 文件加载 Skill

    Args:
        path: skill.yaml 文件路径

    Returns:
        Skill 对象，加载失败返回 None
    """
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return Skill(data, path.parent)
    except Exception:
        return None


def discover_skills(skills_dir: Path) -> list[Skill]:
    """扫描目录，加载所有 Skill

    Args:
        skills_dir: .mokioclaw/skills/ 目录

    Returns:
        Skill 列表
    """
    if not skills_dir.exists():
        return []
    skills = []
    for yaml_path in sorted(skills_dir.glob("*/skill.yaml")):
        skill = load_skill(yaml_path)
        if skill is not None:
            skills.append(skill)
    return skills
