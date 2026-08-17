"""从已启用插件加载 skills / commands / hooks"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.plugins.marketplace import enabled_plugin_paths
from mokioclaw.tools.skill import Skill, discover_skills

logger = get_logger(__name__)


def discover_plugin_skills(workspace: Path | None = None) -> list[Skill]:
    skills: list[Skill] = []
    for root in enabled_plugin_paths(workspace):
        skill_dir = root / "skills"
        if skill_dir.exists():
            for skill in discover_skills(skill_dir):
                # 命名空间：避免与本地 skill 冲突时可加前缀；此处保留 frontmatter name
                skills.append(skill)
    return skills


# 命令名只允许安全字符：名字直接拼进路径，../x 之类可读到目录外的 .md
_SAFE_COMMAND_NAME = re.compile(r"^[A-Za-z0-9_\-一-鿿]+$")


def load_plugin_command(name: str, workspace: Path | None = None) -> str | None:
    if not _SAFE_COMMAND_NAME.match(name or ""):
        return None
    for root in enabled_plugin_paths(workspace):
        path = root / "commands" / f"{name}.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug("plugin command read failed %s: %s", path, exc)
    return None


def list_plugin_command_names(workspace: Path | None = None) -> list[str]:
    names: list[str] = []
    for root in enabled_plugin_paths(workspace):
        cmd_dir = root / "commands"
        if not cmd_dir.exists():
            continue
        for path in sorted(cmd_dir.glob("*.md")):
            if path.stem not in names:
                names.append(path.stem)
    return names


def merge_plugin_hooks_configs(workspace: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """读取启用插件的 hooks.json，结构与项目 hooks 相同"""
    from mokioclaw.core.hook_loader import _read_hooks_file

    merged: dict[str, list[dict[str, Any]]] = {}
    for root in enabled_plugin_paths(workspace):
        data = _read_hooks_file(root / "hooks.json")
        if not data:
            continue
        hooks_block = data.get("hooks", data)
        if not isinstance(hooks_block, dict):
            continue
        for event_name, entries in hooks_block.items():
            if not isinstance(entries, list):
                continue
            merged.setdefault(str(event_name), []).extend(
                e for e in entries if isinstance(e, dict)
            )
    return merged
