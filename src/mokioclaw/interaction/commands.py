"""
斜杠命令解析与分发

对齐 Claude Code / docs/agent.md：
- 系统命令：/help /clear /memory /compact /cost /model /exit — 框架直接执行
- Skill 命令：/skill-name — 读取 SKILL.md 注入 messages
- 自定义命令：.mokioclaw/commands/*.md — 轻量提示词模板
- 未识别：降级为普通用户消息
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.memory.topic_store import TopicStore
from mokioclaw.tools.skill import Skill, discover_skills, load_skill_markdown

logger = get_logger(__name__)


class CommandKind(Enum):
    SYSTEM = "system"
    SKILL = "skill"
    CUSTOM = "custom"
    FALLTHROUGH = "fallthrough"


@dataclass
class CommandResult:
    kind: CommandKind
    name: str
    handled: bool = True
    # 给 UI 展示的提示
    ui_message: str = ""
    # 注入到 agent 的用户消息（技能 / 自定义命令）
    inject_message: str = ""
    # 系统副作用标记
    action: str = ""  # clear | exit | compact | none
    meta: dict[str, Any] = field(default_factory=dict)


# 内置系统命令
_SYSTEM_COMMANDS = {
    "help",
    "clear",
    "memory",
    "compact",
    "cost",
    "model",
    "mode",
    "plugin",
    "plugins",
    "new",
    "exit",
    "quit",
}


def is_slash_command(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("/") and len(stripped) > 1


def parse_slash_command(text: str) -> tuple[str, str]:
    """解析 `/name rest...` → (name, args)"""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return "", text
    body = stripped[1:]
    if not body:
        return "", text
    parts = body.split(None, 1)
    name = parts[0].strip().lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


def dispatch_slash_command(
    text: str,
    *,
    workspace: Path | None = None,
) -> CommandResult:
    """分发斜杠命令

    未识别时返回 FALLTHROUGH，由上层当作普通消息处理。
    """
    if not is_slash_command(text):
        return CommandResult(
            kind=CommandKind.FALLTHROUGH,
            name="",
            handled=False,
            inject_message=text,
        )

    name, args = parse_slash_command(text)
    if not name:
        return CommandResult(kind=CommandKind.FALLTHROUGH, name="", handled=False, inject_message=text)

    if name in _SYSTEM_COMMANDS:
        return _handle_system(name, args, workspace)

    # Skill（SKILL.md 或 skill.yaml）
    skill = _find_skill(name, workspace)
    if skill is not None:
        body = load_skill_markdown(skill) or skill.description
        inject = f"# Skill: {skill.name}\n\n{body}"
        if args:
            inject += f"\n\n## User args\n{args}"
        return CommandResult(
            kind=CommandKind.SKILL,
            name=name,
            ui_message=f"Loaded skill /{name}",
            inject_message=inject,
            action="inject",
        )

    # 自定义命令 .mokioclaw/commands/*.md 或启用插件 commands
    custom = _load_custom_command(name, workspace)
    if custom is None:
        try:
            from mokioclaw.plugins.loader import load_plugin_command

            custom = load_plugin_command(name, workspace)
        except Exception:
            custom = None
    if custom is not None:
        inject = custom
        if args:
            inject = inject.replace("$ARGUMENTS", args)
            if "$ARGUMENTS" not in custom:
                inject = f"{custom.rstrip()}\n\n## User args\n{args}"
        return CommandResult(
            kind=CommandKind.CUSTOM,
            name=name,
            ui_message=f"Loaded command /{name}",
            inject_message=inject,
            action="inject",
        )

    return CommandResult(
        kind=CommandKind.FALLTHROUGH,
        name=name,
        handled=False,
        inject_message=text,
        ui_message=f"Unknown command /{name}; treating as normal message.",
    )


def list_available_commands(workspace: Path | None = None) -> list[str]:
    """供 TUI 补全使用的命令名列表（不含前导 /）"""
    names = sorted(_SYSTEM_COMMANDS)
    for skill in _discover_all_skills(workspace):
        if skill.name and skill.name not in names:
            names.append(skill.name)
    if workspace is not None:
        cmd_dir = workspace / ".mokioclaw" / "commands"
        if cmd_dir.exists():
            for path in sorted(cmd_dir.glob("*.md")):
                if path.stem not in names:
                    names.append(path.stem)
    try:
        from mokioclaw.plugins.loader import list_plugin_command_names

        for name in list_plugin_command_names(workspace):
            if name not in names:
                names.append(name)
    except Exception:
        pass
    return names


def filter_command_suggestions(prefix: str, workspace: Path | None = None, *, limit: int = 12) -> list[str]:
    """按前缀过滤斜杠命令候选（prefix 可带或不带 /）"""
    needle = prefix.lstrip().lstrip("/").lower()
    cmds = list_available_commands(workspace)
    if not needle:
        return cmds[:limit]
    exact = [c for c in cmds if c.lower().startswith(needle)]
    fuzzy = [c for c in cmds if needle in c.lower() and c not in exact]
    return (exact + fuzzy)[:limit]


def _handle_system(name: str, args: str, workspace: Path | None) -> CommandResult:
    if name in {"exit", "quit"}:
        return CommandResult(kind=CommandKind.SYSTEM, name=name, ui_message="Exiting.", action="exit")
    if name == "new":
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message="Starting a new session.",
            action="new",
        )
    if name == "clear":
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message="Context cleared for next turn.",
            action="clear",
        )
    if name == "compact":
        # 写入 workspace 标记，下一次 create_runtime / context_monitor 读取
        if workspace is not None:
            try:
                flag = workspace / ".mokioclaw" / "force_compact.flag"
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("1", encoding="utf-8")
            except OSError:
                pass
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message="Compact requested; next turn will force context compression.",
            action="compact",
        )
    if name == "mode":
        mode = (args or "").strip().lower()
        valid = {"auto", "plan", "approve", "edit", "bypass"}
        if mode not in valid:
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name=name,
                ui_message=f"Usage: /mode <{'|'.join(sorted(valid))}>. Current env MOKIO_AGENT_MODE.",
                action="none",
            )
        if workspace is not None:
            try:
                path = workspace / ".mokioclaw" / "agent_mode"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(mode, encoding="utf-8")
            except OSError:
                pass
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message=f"Agent mode set to '{mode}' for this workspace.",
            action="none",
            meta={"agent_mode": mode},
        )
    if name == "help":
        cmds = ", ".join(f"/{c}" for c in list_available_commands(workspace))
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message=f"Available commands: {cmds}",
            action="none",
        )
    if name == "memory":
        return _memory_command(workspace)
    if name == "cost":
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message="Token cost tracking is available via session trace files under .mokioclaw/traces/.",
            action="none",
        )
    if name == "model":
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message=f"Current model from env. Args ignored for now: {args or '(none)'}",
            action="none",
        )
    if name in {"plugin", "plugins"}:
        return _plugin_command(args, workspace)
    return CommandResult(kind=CommandKind.FALLTHROUGH, name=name, handled=False, inject_message=f"/{name} {args}".strip())


def _plugin_command(args: str, workspace: Path | None) -> CommandResult:
    from mokioclaw.plugins.marketplace import (
        disable_plugin,
        enable_plugin,
        install_plugin,
        list_catalog,
        list_installed,
        uninstall_plugin,
    )

    parts = (args or "").split()
    sub = parts[0].lower() if parts else "list"
    rest = parts[1:]

    def _plugin_name(tokens: list[str]) -> str:
        """提取第一个非 flag token 作为插件名（跳过 --project 等）"""
        return next((x for x in tokens if not x.startswith("-")), "")

    if sub in {"list", "ls", ""}:
        rows = []
        for p in list_catalog(workspace):
            flags = []
            if p.installed:
                flags.append("installed")
            if p.enabled:
                flags.append("enabled")
            if p.source == "builtin":
                flags.append("builtin")
            mark = ",".join(flags) if flags else "available"
            rows.append(f"- {p.name}@{p.version} [{mark}] — {p.description}")
        body = "\n".join(rows) or "(empty marketplace)"
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="plugin",
            ui_message=f"# Plugins\n\n{body}\n\nUsage: /plugin install <name> | enable <name> | disable <name>",
            action="none",
        )

    if sub == "install" and rest:
        scope = "project" if "--project" in rest else "user"
        name = _plugin_name(rest)
        result = install_plugin(name, workspace=workspace, scope=scope)
        if not result.get("ok"):
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name="plugin",
                ui_message=f"Install failed: {result.get('error')}",
                action="none",
            )
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="plugin",
            ui_message=f"Installed and enabled plugin '{name}' → {result.get('path')}",
            action="none",
            meta=result,
        )

    if sub == "enable" and rest:
        name = _plugin_name(rest)
        result = enable_plugin(name, workspace=workspace)
        msg = f"Enabled '{name}'" if result.get("ok") else f"Enable failed: {result.get('error')}"
        return CommandResult(kind=CommandKind.SYSTEM, name="plugin", ui_message=msg, action="none")

    if sub == "disable" and rest:
        name = _plugin_name(rest)
        result = disable_plugin(name, workspace=workspace)
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="plugin",
            ui_message=f"Disabled '{name}'",
            action="none",
            meta=result,
        )

    if sub == "uninstall" and rest:
        name = _plugin_name(rest)
        result = uninstall_plugin(name, workspace=workspace)
        if not result.get("ok"):
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name="plugin",
                ui_message=f"Uninstall failed: {result.get('error')}",
                action="none",
            )
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="plugin",
            ui_message=f"Uninstalled plugin '{name}'",
            action="none",
            meta=result,
        )

    if sub == "info" and rest:
        name = _plugin_name(rest)
        catalog = {p.name: p for p in list_catalog(workspace)}
        info = catalog.get(name)
        if info is None:
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name="plugin",
                ui_message=f"Unknown plugin: {name}",
                action="none",
            )
        flags = []
        if info.installed:
            flags.append("installed")
        if info.enabled:
            flags.append("enabled")
        if info.source == "builtin":
            flags.append("builtin")
        meta_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(info.meta.items()) if k not in {"name", "path"})
        msg = (
            f"# Plugin {info.name}\n\n"
            f"- version: {info.version}\n"
            f"- description: {info.description}\n"
            f"- source: {info.source}\n"
            f"- state: {','.join(flags) or 'available'}\n"
            f"- path: {info.path}\n"
        )
        if meta_lines:
            msg += f"\n# manifest\n{meta_lines}"
        return CommandResult(kind=CommandKind.SYSTEM, name="plugin", ui_message=msg, action="none")

    if sub == "installed":
        rows = [
            f"- {p.name}@{p.version} {'(enabled)' if p.enabled else '(disabled)'} @ {p.path}"
            for p in list_installed(workspace)
        ]
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="plugin",
            ui_message="# Installed plugins\n\n" + ("\n".join(rows) or "(none)"),
            action="none",
        )

    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="plugin",
        ui_message="Usage: /plugin list|install <name>|enable <name>|disable <name>|uninstall <name>|info <name>|installed",
        action="none",
    )


def _memory_command(workspace: Path | None) -> CommandResult:
    if workspace is None:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="memory",
            ui_message="No workspace; cannot list memory.",
            action="none",
        )
    store = TopicStore(workspace)
    index = store.load_index().strip() or "(empty MEMORY.md)"
    topics = store.list_topics()
    topic_lines = "\n".join(f"- {t.name} ({t.topic_type}): {t.description}" for t in topics) or "(no topics)"
    msg = f"# Memory Index\n\n{index}\n\n# Topics\n\n{topic_lines}"
    return CommandResult(kind=CommandKind.SYSTEM, name="memory", ui_message=msg, action="none")


def _discover_all_skills(workspace: Path | None) -> list[Skill]:
    skills: list[Skill] = []
    dirs: list[Path] = [Path.home() / ".mokioclaw" / "skills"]
    if workspace is not None:
        dirs.append(workspace / ".mokioclaw" / "skills")
    for d in dirs:
        skills.extend(discover_skills(d))
    try:
        from mokioclaw.plugins.loader import discover_plugin_skills

        skills.extend(discover_plugin_skills(workspace))
    except Exception:
        pass
    return skills


def _find_skill(name: str, workspace: Path | None) -> Skill | None:
    for skill in _discover_all_skills(workspace):
        if skill.name.lower() == name.lower():
            return skill
    return None


def _load_custom_command(name: str, workspace: Path | None) -> str | None:
    if workspace is None:
        return None
    path = workspace / ".mokioclaw" / "commands" / f"{name}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Failed to read custom command %s: %s", path, exc)
        return None
