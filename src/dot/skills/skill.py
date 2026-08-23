"""
Skill 定义与加载

Skill 是懒加载的任务 SOP / 领域知识手册包（对齐 Claude Code）：
- 会话启动只扫元数据
- 被 /skill 或 SkillTool 触发后，才把完整正文注入 messages

支持两种磁盘格式：
1. Claude Code 风格：.mokioclaw/skills/<name>/SKILL.md（YAML frontmatter + markdown）
2. 兼容旧格式：.mokioclaw/skills/<name>/skill.yaml
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core.log import get_logger

logger = get_logger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


class Skill:
    """Skill 定义"""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        # 优先 frontmatter name；否则用目录名（SKILL.md / skill.yaml 的 parent）
        explicit = str(data.get("name", "") or "").strip()
        if explicit:
            self.name = explicit
        else:
            self.name = path.parent.name if path.name.lower() in {"skill.md", "skill.yaml", "skill.yml"} else path.stem
        if not self.name:
            self.name = path.parent.name
        self.description: str = str(data.get("description", ""))
        self.tools: list[str] = list(data.get("tools", []) or [])
        self.prompts: list[str] = list(data.get("prompts", []) or [])
        self.triggers: list[dict[str, str]] = list(data.get("triggers", []) or [])
        # auto | manual（对齐 Claude Code invoke 字段）
        if data.get("disable-model-invocation") in (True, "true", "yes"):
            self.invoke = "manual"
        else:
            self.invoke = str(data.get("invoke", "auto") or "auto")
        self.path = path
        self._raw = data
        self._body: str | None = data.get("_body")

    def matches_intent(self, intent: str) -> bool:
        for trigger in self.triggers:
            if trigger.get("intent") == intent:
                return True
        return False

    def matches_keyword(self, text: str) -> bool:
        text_lower = text.lower()
        for trigger in self.triggers:
            keyword = trigger.get("keyword", "")
            if keyword and keyword.lower() in text_lower:
                return True
        # 也匹配 description 关键词（粗粒度 auto）
        if self.invoke == "auto" and self.description:
            for token in re.findall(r"[a-zA-Z0-9_\-]{4,}", self.description.lower()):
                if token in text_lower:
                    return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "prompts": self.prompts,
            "triggers": self.triggers,
            "invoke": self.invoke,
        }


def load_skill(path: Path) -> Skill | None:
    """从 skill.yaml 或 SKILL.md 加载 Skill 元数据"""
    if not path.exists():
        return None
    name = path.name.lower()
    if name == "skill.md":
        return _load_skill_md(path)
    if name.endswith((".yaml", ".yml")):
        return _load_skill_yaml(path)
    return None


def load_skill_markdown(skill: Skill) -> str:
    """读取 Skill 完整正文（触发时调用）"""
    if skill._body is not None:
        return skill._body
    path = skill.path
    if path.name.lower() == "skill.md":
        text = _read_text(path)
        _, body = _split_frontmatter(text)
        return body.strip()
    # yaml 格式：同目录 README.md / SKILL.md 作为正文
    for candidate in (path.parent / "SKILL.md", path.parent / "README.md"):
        if candidate.exists():
            text = _read_text(candidate)
            _, body = _split_frontmatter(text)
            return body.strip() or skill.description
    return skill.description


def discover_skills(skills_dir: Path) -> list[Skill]:
    """扫描目录，加载所有 Skill（仅元数据）

    支持嵌套目录（**/SKILL.md），兼容旧的 mokioclaw 插件目录结构。
    """
    if not skills_dir.exists():
        return []
    skills: list[Skill] = []
    seen: set[str] = set()
    # 递归扫描 SKILL.md（支持嵌套目录，如 plugins/builtin/xxx/skills/review/SKILL.md）
    for md_path in sorted(skills_dir.rglob("SKILL.md")):
        skill = load_skill(md_path)
        if skill is not None and skill.name not in seen:
            skills.append(skill)
            seen.add(skill.name)
    for yaml_path in sorted(skills_dir.rglob("skill.yaml")):
        skill = load_skill(yaml_path)
        if skill is not None and skill.name not in seen:
            skills.append(skill)
            seen.add(skill.name)
    return skills


def build_skill_catalog(skills: list[Skill]) -> str:
    """生成注入 system 动态区的技能目录（仅 name + description）"""
    if not skills:
        return ""
    lines = ["Available skills (invoke with /name or SkillTool):"]
    for skill in skills:
        inv = skill.invoke or "auto"
        lines.append(f"- /{skill.name} [{inv}]: {skill.description}")
    return "\n".join(lines)


def _load_skill_yaml(path: Path) -> Skill | None:
    if yaml is None:
        logger.warning("PyYAML not installed; cannot load %s", path)
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return Skill(data, path)
    except Exception as exc:
        logger.debug("Failed to load skill yaml %s: %s", path, exc)
        return None


def _load_skill_md(path: Path) -> Skill | None:
    text = _read_text(path)
    if not text:
        return None
    fm, body = _split_frontmatter(text)
    data = dict(fm)
    data.setdefault("name", path.parent.name)
    data["_body"] = body.strip()
    return Skill(data, path)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    meta: dict[str, Any] = {}
    if yaml is not None:
        try:
            parsed = yaml.safe_load(fm_text)
            if isinstance(parsed, dict):
                meta = parsed
                return meta, body
        except Exception:
            pass
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Failed to read %s: %s", path, exc)
        return ""
