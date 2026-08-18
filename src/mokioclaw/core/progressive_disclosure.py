"""渐进式提示词披露（Progressive Disclosure）

AI 在回复中插入特殊标记触发按需加载：
  [NEED_MCP: tool_name]    — 加载 MCP 工具的完整 schema
  [NEED_SKILL: skill_name] — 加载 Skill 的完整 markdown

运行时检测到标记后：
1. 从对应注册表加载完整定义
2. 注入 runtime 的 deferred_tool_catalog（system prompt 动态区）
3. 记录到已加载集合，避免重复加载
"""
from __future__ import annotations

import re
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 标记正则：[NEED_MCP: tool_name] 或 [NEED_SKILL: skill_name]
_MARKER_RE = re.compile(r"\[NEED_(MCP|SKILL):\s*([^\]]+?)\]")

# 已加载的 MCP 工具（避免重复加载）
_LOADED_MCP: dict[str, str] = {}
# 已加载的 Skills
_LOADED_SKILLS: dict[str, str] = {}


def parse_markers(text: str) -> list[dict[str, str]]:
    """从文本中提取所有渐进式披露标记

    Args:
        text: AI 回复文本

    Returns:
        标记列表，每项含 {"type": "mcp"|"skill", "name": "..."}
    """
    results: list[dict[str, str]] = []
    for match in _MARKER_RE.finditer(text):
        kind = match.group(1).lower()
        name = match.group(2).strip()
        if kind and name:
            results.append({"type": kind, "name": name})
    return results


def resolve_mcp_tool(workspace: Any, tool_name: str) -> str | None:
    """加载 MCP 工具的完整 schema 描述

    Args:
        workspace: workspace Path
        tool_name: 工具名称（可能是简写）

    Returns:
        工具定义文本，未找到返回 None
    """
    if tool_name in _LOADED_MCP:
        return _LOADED_MCP[tool_name]
    try:
        from mokioclaw.mcp.bridge import get_mcp_bridge
        from pathlib import Path
        ws = workspace if isinstance(workspace, Path) else Path(str(workspace))
        bridge = get_mcp_bridge(ws)
        tools = {t.name: t for t in bridge.to_langchain_tools()}
        # 精确匹配
        tool = tools.get(tool_name)
        if tool is None:
            # 模糊匹配：mcp__server__tool 或 server:tool
            for name, t in tools.items():
                if name.endswith(f"__{tool_name}") or name.endswith(f":{tool_name}"):
                    tool = t
                    break
        if tool is None:
            logger.debug("MCP tool not found: %s", tool_name)
            return None
        desc = (
            f"### MCP Tool: {tool.name}\n"
            f"Description: {tool.description}\n"
            f"Parameters: {getattr(tool, 'args_schema', None) or 'see schema'}"
        )
        _LOADED_MCP[tool_name] = desc
        return desc
    except Exception as exc:
        logger.debug("MCP tool resolve failed: %s", exc)
        return None


def resolve_skill(skill_name: str, workspace: Any = None) -> str | None:
    """加载 Skill 的完整 markdown 内容

    Args:
        skill_name: skill 名称
        workspace: 可选 workspace

    Returns:
        skill markdown 内容，未找到返回 None
    """
    if skill_name in _LOADED_SKILLS:
        return _LOADED_SKILLS[skill_name]
    try:
        from mokioclaw.tools.skill import discover_skills, load_skill_markdown
        from pathlib import Path
        ws = workspace if isinstance(workspace, Path) else None
        dirs = []
        if ws:
            dirs.append(ws / ".mokioclaw" / "skills")
        dirs.append(Path.home() / ".mokioclaw" / "skills")
        for d in dirs:
            for skill in discover_skills(d):
                if skill.name.lower() == skill_name.lower():
                    body = load_skill_markdown(skill) or skill.description
                    _LOADED_SKILLS[skill_name] = body
                    return body
        logger.debug("Skill not found: %s", skill_name)
        return None
    except Exception as exc:
        logger.debug("Skill resolve failed: %s", exc)
        return None


def process_markers(
    text: str,
    runtime: Any,
    workspace: Any,
) -> str:
    """处理文本中的渐进式披露标记，返回增强后的文本

    检测到标记后：
    1. 加载完整定义
    2. 注入 runtime.deferred_tool_catalog（供下一轮 system prompt 动态区使用）

    Args:
        text: AI 回复文本
        runtime: RuntimeState
        workspace: workspace Path

    Returns:
        增强后的文本（标记已移除，完整定义追加到末尾）
    """
    markers = parse_markers(text)
    if not markers:
        return text

    additions: list[str] = []
    for marker in markers:
        if marker["type"] == "mcp":
            desc = resolve_mcp_tool(workspace, marker["name"])
            if desc:
                additions.append(desc)
        elif marker["type"] == "skill":
            body = resolve_skill(marker["name"], workspace)
            if body:
                additions.append(f"### Skill: {marker['name']}\n{body}")

    if not additions:
        return text

    # 注入 runtime 供下一轮使用
    extra = "\n\n".join(additions)
    existing = str(getattr(runtime, "deferred_tool_catalog", "") or "").strip()
    runtime.deferred_tool_catalog = (existing + "\n\n" + extra).strip()

    # 返回原文（标记保留在对话记录中）
    return text
