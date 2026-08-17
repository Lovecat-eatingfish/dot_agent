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

# 项目级配置文件名（.mokioclaw/config.md 或项目根 CLAUDE.md / CLAUDE.local.md）
_PROJECT_CONFIG_NAME = "config.md"
_PROJECT_CONFIG_DIR = ".mokioclaw"
_PROJECT_CLAUDE_MD = "CLAUDE.md"
_PROJECT_CLAUDE_LOCAL = "CLAUDE.local.md"


@dataclass
class UserConfig:
    """用户配置，合并全局 + 项目配置后的结果

    所有字段都有默认值，缺失时使用内置默认。
    """
    # --- 行为配置 ---
    approval_mode: str = "inline"
    agent_mode: str = "auto"  # auto | plan | approve | edit
    checkpoint_mode: str = "light"
    trace_mode: str = "on"

    # --- 工具权限配置 ---
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    # --- Bash 配置 ---
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000

    # --- 工作流配置 ---
    max_attempts: int = 3
    context_token_limit: int = 400000

    # --- 自定义指令（markdown 正文，注入到所有 agent prompt） ---
    custom_instructions: str = ""

    # --- 文件作用域规则（rules/*.md 带 globs frontmatter，读取匹配文件时注入） ---
    # 每条：{"name": 文件名, "globs": ["**/*.py", ...], "body": 规则正文}
    glob_rules: list[dict[str, Any]] = field(default_factory=list)

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

    # 1. 加载全局配置（默认 ~/.mokioclaw/CLAUDE.md；显式 override 可强制指定路径或传 Path("") 禁用）
    global_path = global_override if global_override is not None else Path.home() / ".mokioclaw" / "CLAUDE.md"
    if str(global_path) and global_path.exists():
        try:
            frontmatter, body = _parse_markdown_with_frontmatter(global_path)
            _apply_frontmatter(config, frontmatter)
            if body.strip():
                config.custom_instructions = _merge_instructions(config.custom_instructions, body)
            sources.append(f"global:{global_path}")
        except Exception as exc:
            logger.debug("global config load skipped (%s): %s", global_path, exc)

    # 2. 加载项目级配置（递归 CLAUDE.md / CLAUDE.local.md / .mokioclaw/config.md）
    # 应用顺序：父目录 → 子目录（后应用者覆盖先应用者），越靠近 workspace 优先级越高（对齐 Claude Code）
    project_root = workspace or Path.cwd()
    project_sources = list(reversed(_discover_project_config_sources(project_root, override=project_override)))
    for source_path in project_sources:
        try:
            frontmatter, body = _parse_markdown_with_frontmatter(source_path)
            _apply_frontmatter(config, frontmatter)
            if body.strip():
                config.custom_instructions = _merge_instructions(config.custom_instructions, body)
            sources.append(f"project:{source_path}")
        except Exception as exc:
            logger.debug("project config load skipped (%s): %s", source_path, exc)

    # 2b. 加载模块化规则文件（.claude/rules/*.md 或 .mokioclaw/rules/*.md）
    # globs frontmatter（对齐 Claude Code）：带 globs 的规则只在读取匹配文件时注入
    #（file_tools.read_file 调用 matching_glob_rules），不带 globs 的仍无条件合并
    rule_files = _discover_rules_dir(project_root)
    for rule_path in rule_files:
        try:
            frontmatter, body = _parse_markdown_with_frontmatter(rule_path)
            if body.strip():
                globs_raw = frontmatter.get("globs") if isinstance(frontmatter, dict) else None
                globs = _coerce_string_list(globs_raw) if globs_raw else []
                if globs:
                    config.glob_rules.append({"name": rule_path.name, "globs": globs, "body": body.strip()})
                else:
                    config.custom_instructions = _merge_instructions(config.custom_instructions, body)
            sources.append(f"rules:{rule_path}")
        except Exception as exc:
            logger.debug("rules file load skipped (%s): %s", rule_path, exc)

    # 3. 加载运行时权限规则（.mokioclaw/permissions.json，由 /permissions 命令维护）
    if workspace is not None:
        perms_path = workspace / ".mokioclaw" / "permissions.json"
        if perms_path.exists():
            try:
                import json
                data = json.loads(perms_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if data.get("allowed_tools"):
                        config.allowed_tools = _coerce_string_list(data["allowed_tools"])
                    if data.get("disallowed_tools"):
                        config.disallowed_tools = _coerce_string_list(data["disallowed_tools"])
                    sources.append(f"permissions:{perms_path}")
            except Exception as exc:
                logger.debug("permissions.json load skipped (%s): %s", perms_path, exc)

    config.config_sources = sources
    return config


def _expand_body_imports(body: str, source: Path, *, max_depth: int = 5) -> str:
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("@") or line.startswith("@{"):
            lines.append(raw_line)
            continue
        imported = _resolve_import_path(source, line[1:].strip())
        if imported is None:
            lines.append(raw_line)
            continue
        imported_text = _read_imported_text(imported, max_depth=max_depth - 1 if max_depth > 0 else 0)
        if imported_text:
            lines.append(imported_text.rstrip())
    return "\n".join(lines)

def _resolve_import_path(source: Path, import_ref: str) -> Path | None:
    candidate = Path(import_ref.strip())
    if not candidate.is_absolute():
        candidate = (source.parent / candidate).resolve()
    return candidate if candidate.exists() else None


def _read_imported_text(path: Path, *, max_depth: int) -> str:
    if max_depth < 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if max_depth == 0:
        return text
    if "@" not in text:
        return text
    return _expand_body_imports(text, path, max_depth=max_depth)


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
    body = _expand_body_imports(text[end + 3:].strip(), path)

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
        "agent_mode": "agent_mode",
        "mode": "agent_mode",
        "checkpoint_mode": "checkpoint_mode",
        "trace_mode": "trace_mode",
        "allowed_tools": "allowed_tools",
        "allowedTools": "allowed_tools",
        "disallowed_tools": "disallowed_tools",
        "disallowedTools": "disallowed_tools",
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
            # YAML bool 需要先转为字符串，再按目标类型转换
            if isinstance(value, bool):
                value = str(value).lower()
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
            elif isinstance(current, list):
                setattr(config, attr, _coerce_string_list(value))


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _merge_instructions(existing: str, new: str) -> str:
    """合并自定义指令（新指令追加到已有指令之后）"""
    existing = existing.strip()
    new = new.strip()
    if not existing:
        return new
    if not new:
        return existing
    return f"{existing}\n\n{new}"


def _discover_rules_dir(workspace: Path) -> list[Path]:
    """Discover modular rule files from .claude/rules/ or .mokioclaw/rules/.

    Aligns with Claude Code's .claude/rules/ directory:
    - Each .md file is a standalone rule module
    - Rules are merged into custom_instructions in alphabetical order
    """
    rule_files: list[Path] = []
    visited: set[Path] = set()
    for rules_dir_name in (".claude/rules", ".mokioclaw/rules"):
        current = workspace.resolve()
        for _ in range(6):
            rules_dir = current / rules_dir_name
            if rules_dir.is_dir():
                for md_file in sorted(rules_dir.glob("*.md")):
                    resolved = md_file.resolve()
                    if resolved not in visited:
                        visited.add(resolved)
                        rule_files.append(md_file)
            if (current / ".git").exists():
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
    return rule_files


def _discover_project_config_sources(workspace: Path, *, override: Path | None = None) -> list[Path]:
    sources: list[Path] = []
    if override is not None:
        if override.exists():
            sources.append(override)
        return sources

    visited: set[Path] = set()
    current = workspace.resolve()
    for _ in range(6):
        for candidate in (
            current / _PROJECT_CONFIG_DIR / _PROJECT_CONFIG_NAME,
            current / _PROJECT_CLAUDE_MD,
            current / _PROJECT_CLAUDE_LOCAL,
        ):
            if candidate.exists():
                resolved = candidate.resolve()
                if resolved not in visited:
                    visited.add(resolved)
                    sources.append(candidate)
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return sources


def _find_project_config(workspace: Path) -> Path | None:
    """从 workspace 向上查找项目级配置文件

    查找 `.mokioclaw/config.md`，最多向上 5 层。
    """
    current = workspace.resolve()
    for _ in range(6):  # workspace + 5 parent dirs
        candidate = current / _PROJECT_CONFIG_DIR / _PROJECT_CONFIG_NAME
        if candidate.exists():
            return candidate
        # 遇到 .git 根目录停止（兼容 worktree：.git 可能是文件）
        if (current / ".git").exists():
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




def _glob_match(path: Path, pattern: str) -> bool:
    """单条 glob 匹配：依次尝试相对路径 / 绝对路径 / 文件名

    PurePath.full_match（Python 3.13+）支持 ** 跨目录语义；
    老模式（如 *.py 只匹配文件名）用 fnmatch 兜底。
    """
    import fnmatch
    from pathlib import PurePath

    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return False
    rel = path.as_posix()
    name = path.name
    candidates = (rel, str(path), name)
    try:
        return any(PurePath(candidate).full_match(pattern) for candidate in candidates)
    except (AttributeError, ValueError):
        # full_match 不可用（<3.13）或模式非法时退化为 fnmatch
        return any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates)


def matching_glob_rules(workspace: Path | None, path: Path) -> list[dict[str, Any]]:
    """返回 globs 命中给定文件路径的规则列表（读取文件时注入用）

    Args:
        workspace: 工作区（定位 rules 目录）
        path: 被读取文件的绝对路径

    Returns:
        [{"name": ..., "globs": [...], "body": ...}, ...]
    """
    config = _glob_rules_config(workspace)
    # 相对化路径，让 **/*.py 也能命中 src/pkg/mod.py 这类嵌套路径
    try:
        rel = path.resolve().relative_to(workspace.resolve()) if workspace else path
    except (ValueError, OSError):
        rel = path
    matched = [rule for rule in config.glob_rules if any(_glob_match(rel, g) for g in rule.get("globs", []))]
    return matched


# glob_rules 缓存（key=workspace）：FileRead 高频调用，避免每次全量解析配置；
# 按 (文件名, mtime) 组合签名失效，会话中增删改 rules 文件后无需重启
_glob_rules_cache: dict[str, tuple[tuple[tuple[str, float], ...], list[dict[str, Any]]]] = {}


def _glob_rules_config(workspace: Path | None) -> UserConfig:
    """读取并缓存 glob_rules，返回仅携带该字段的轻量 UserConfig"""
    key = str(workspace.resolve()) if workspace is not None else ""
    rule_files = _discover_rules_dir(workspace) if workspace is not None else []
    try:
        signature = tuple((p.name, p.stat().st_mtime) for p in rule_files)
    except OSError:
        signature = ()
    cached = _glob_rules_cache.get(key)
    if cached is not None and cached[0] == signature:
        return UserConfig(glob_rules=cached[1])
    try:
        rules = list(load_user_config(workspace).glob_rules)
    except Exception:
        rules = []
    _glob_rules_cache[key] = (signature, rules)
    return UserConfig(glob_rules=rules)


def format_glob_rules_for_read(rules: list[dict[str, Any]]) -> str:
    """把命中的规则格式化为附加到 FileRead 结果尾部的说明块"""
    if not rules:
        return ""
    blocks = [
        f'<file-rules source="{rule.get("name", "rules")}">\n{rule.get("body", "")}\n</file-rules>'
        for rule in rules
    ]
    return "\n".join(blocks)
