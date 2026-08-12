"""
用户配置加载模块

加载两级配置文件，合并为统一的 UserConfig：

1. 全局配置：~/.mokioclaw/CLAUDE.md
2. 项目配置：.mokioclaw/config.md（从 workspace 向上查找）

格式：YAML frontmatter + markdown body
```markdown
---
approval_mode: inline
bash_timeout: 120
max_attempts: 3
---

# Custom Instructions

These are appended to all agent system prompts.
```

合并规则：项目配置覆盖全局配置的相同字段。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 全局配置目录
_GLOBAL_DIR = Path.home() / ".mokioclaw"
_GLOBAL_CONFIG = _GLOBAL_DIR / "CLAUDE.md"

# 项目级配置文件名
_PROJECT_CONFIG_NAME = "config.md"
_PROJECT_CONFIG_DIR = ".mokioclaw"


@dataclass
class UserConfig:
    """用户配置，合并全局 + 项目配置后的结果

    所有字段都有默认值，缺失时使用内置默认。
    """
    # --- 行为配置 ---
    approval_mode: str = "inline"
    checkpoint_mode: str = "light"
    trace_mode: str = "on"

    # --- Bash 配置 ---
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000

    # --- 工作流配置 ---
    max_attempts: int = 3
    context_token_limit: int = 400000

    # --- 自定义指令（markdown 正文，注入到所有 agent prompt） ---
    custom_instructions: str = ""

    # --- 元数据 ---
    config_sources: list[str] = field(default_factory=list)


def load_user_config(
    workspace: Path | None = None,
    *,
    global_override: Path | None = None,
    project_override: Path | None = None,
) -> UserConfig:
    """加载并合并用户配置

    加载顺序（后者覆盖前者）：
    1. UserConfig 默认值
    2. ~/.mokioclaw/CLAUDE.md（全局）
    3. .mokioclaw/config.md（项目级，从 workspace 向上查找）

    Args:
        workspace: 工作区路径，用于查找项目级配置
        global_override: 强制指定全局配置文件路径
        project_override: 强制指定项目级配置文件路径

    Returns:
        合并后的 UserConfig
    """
    config = UserConfig()
    sources: list[str] = []

    # 1. 加载全局配置
    global_path = global_override or _GLOBAL_CONFIG
    if global_path.exists():
        try:
            frontmatter, body = _parse_markdown_with_frontmatter(global_path)
            _apply_frontmatter(config, frontmatter)
            if body.strip():
                config.custom_instructions = _merge_instructions(config.custom_instructions, body)
            sources.append(f"global:{global_path}")
        except Exception as exc:
            logger.debug("global config load skipped (%s): %s", global_path, exc)

    # 2. 加载项目级配置
    project_path = project_override
    if project_path is None and workspace is not None:
        project_path = _find_project_config(workspace)
    if project_path is None:
        project_path = _find_project_config(Path.cwd())

    if project_path and project_path.exists():
        try:
            frontmatter, body = _parse_markdown_with_frontmatter(project_path)
            _apply_frontmatter(config, frontmatter)
            if body.strip():
                config.custom_instructions = _merge_instructions(config.custom_instructions, body)
            sources.append(f"project:{project_path}")
        except Exception as exc:
            logger.debug("project config load skipped (%s): %s", project_path, exc)

    config.config_sources = sources
    return config


def _parse_markdown_with_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """解析带 YAML frontmatter 的 markdown 文件

    Returns:
        (frontmatter_dict, body_text)
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text

    # 找到 closing ---
    end = text.find("---", 3)
    if end == -1:
        return {}, text

    frontmatter_raw = text[3:end].strip()
    body = text[end + 3:].strip()

    frontmatter: dict[str, Any] = {}
    if frontmatter_raw:
        try:
            import yaml
            frontmatter = yaml.safe_load(frontmatter_raw) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except ImportError:
            # yaml not available, parse simple key: value lines
            for line in frontmatter_raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, value = line.partition(":")
                    frontmatter[key.strip()] = _coerce_value(value.strip())
        except Exception as exc:
            logger.debug("yaml parse error in %s: %s", path, exc)

    return frontmatter, body


def _coerce_value(raw: str) -> Any:
    """将字符串值转换为适当的类型"""
    if raw.lower() in {"true", "yes", "on"}:
        return True
    if raw.lower() in {"false", "no", "off"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _apply_frontmatter(config: UserConfig, frontmatter: dict[str, Any]) -> None:
    """将 frontmatter 字段应用到 config"""
    # 已知字段映射
    field_map = {
        "approval_mode": "approval_mode",
        "checkpoint_mode": "checkpoint_mode",
        "trace_mode": "trace_mode",
        "bash_timeout": "bash_default_timeout_seconds",
        "bash_default_timeout": "bash_default_timeout_seconds",
        "bash_max_timeout": "bash_max_timeout_seconds",
        "bash_max_output_chars": "bash_max_output_chars",
        "max_attempts": "max_attempts",
        "context_token_limit": "context_token_limit",
    }

    for key, value in frontmatter.items():
        if key in field_map:
            attr = field_map[key]
            current = getattr(config, attr)
            if isinstance(current, bool):
                setattr(config, attr, bool(value))
            elif isinstance(current, int):
                try:
                    setattr(config, attr, int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(current, str):
                setattr(config, attr, str(value))


def _merge_instructions(existing: str, new: str) -> str:
    """合并自定义指令（新指令追加到已有指令之后）"""
    existing = existing.strip()
    new = new.strip()
    if not existing:
        return new
    if not new:
        return existing
    return f"{existing}\n\n{new}"


def _find_project_config(workspace: Path) -> Path | None:
    """从 workspace 向上查找项目级配置文件

    查找 `.mokioclaw/config.md`，最多向上 5 层。
    """
    current = workspace.resolve()
    for _ in range(6):  # workspace + 5 parent dirs
        candidate = current / _PROJECT_CONFIG_DIR / _PROJECT_CONFIG_NAME
        if candidate.exists():
            return candidate
        # 遇到 .git 根目录停止
        if (current / ".git").is_dir():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def get_user_config_paths() -> dict[str, Path | None]:
    """返回配置文件的搜索路径（用于诊断/帮助）"""
    return {
        "global": _GLOBAL_CONFIG if _GLOBAL_CONFIG.exists() else None,
        "project": _find_project_config(Path.cwd()),
    }
