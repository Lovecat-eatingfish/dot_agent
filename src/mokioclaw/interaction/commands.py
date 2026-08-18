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
import os
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import project_memory_dir
from mokioclaw.memory.topic_store import TopicStore
from mokioclaw.tools.skill import Skill, discover_skills, load_skill_markdown

_DISABLE_BUNDLED = os.getenv("MOKIO_DISABLE_BUNDLED_SKILLS", "").lower() in ("1", "true", "yes")

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
    "resume",
    "continue",
    "rollback",
    "sessions",
    "status",
    "permissions",
    "export",
    "branch",
    "cd",
    "loop",
    "batch",
    "init",
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
    if not _DISABLE_BUNDLED:
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
    """Filter slash command candidates by prefix (supports fuzzy matching)."""
    needle = prefix.lstrip().lstrip("/").lower()
    cmds = list_available_commands(workspace)
    if not needle:
        # 空 prefix：高价值命令优先（系统命令已有 24 个，纯字母序会把 plugin 等截出前 12）
        priority = [
            "help", "new", "resume", "continue", "compact", "plugin",
            "mode", "model", "memory", "status", "sessions", "rollback",
        ]
        ordered = [c for c in priority if c in cmds] + [c for c in cmds if c not in priority]
        return ordered[:limit]
    exact = [c for c in cmds if c.lower().startswith(needle)]
    contains = [c for c in cmds if needle in c.lower() and c not in exact]
    # fuzzy: subsequence match (e.g. "pr" matches "permissions")
    subseq = []
    for c in cmds:
        if c in exact or c in contains:
            continue
        ci = 0
        for ch in c.lower():
            if ci < len(needle) and ch == needle[ci]:
                ci += 1
        if ci == len(needle):
            subseq.append(c)
    return (exact + contains + subseq)[:limit]


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
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name=name,
            ui_message=_help_card(workspace),
            action="none",
        )
    if name == "memory":
        return _memory_command(workspace)
    if name == "cost":
        return _cost_command(workspace)
    if name == "model":
        return _model_command(args, workspace)
    if name in {"plugin", "plugins"}:
        return _plugin_command(args, workspace)
    if name == "continue":
        result = _resume_command(args, workspace)
        result.name = "continue"
        return result
    if name == "resume":
        return _resume_command(args, workspace)
    if name == "rollback":
        return _rollback_command(args, workspace)
    if name == "sessions":
        return _sessions_command(workspace)
    if name == "status":
        return _status_command(workspace)
    if name == "permissions":
        return _permissions_command(args, workspace)
    if name == "export":
        return _export_command(args, workspace)
    if name == "branch":
        return _branch_command(args, workspace)
    if name == "cd":
        return _cd_command(args, workspace)
    if name == "loop":
        return _loop_command(args, workspace)
    if name == "batch":
        return _batch_command(args, workspace)
    if name == "init":
        return _init_command(args, workspace)
    if name == "review":
        return _review_command(args, workspace)
    return CommandResult(kind=CommandKind.FALLTHROUGH, name=name, handled=False, inject_message=f"/{name} {args}".strip())


def _help_card(workspace: Path | None) -> str:
    commands = list_available_commands(workspace)
    core = ["help", "clear", "new", "resume", "sessions", "rollback", "exit"]
    runtime = ["mode", "model", "memory", "compact", "cost", "plugin", "plugins"]
    custom = [command for command in commands if command not in set(core + runtime)]
    sections = [
        ("Core", [_command_line(core, commands)]),
        ("Runtime", [_command_line(runtime, commands)]),
    ]
    if custom:
        sections.append(("Project / Skills", [_command_line(custom, commands)]))
    return _card_sections("Help", ["Use slash commands to control the current coding session."], sections)


def _command_line(names: list[str], available: list[str]) -> str:
    available_set = set(available)
    items = [f"/{name}" for name in names if name in available_set]
    return ", ".join(items) if items else "(none)"


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
            ui_message=_card("Memory", ["No workspace; cannot list memory."]),
            action="none",
        )
    from mokioclaw.config.loader import load_user_config
    from mokioclaw.core.paths import project_memory_dir
    from mokioclaw.reliability.session_store import get_latest_session, list_sessions

    store = TopicStore(workspace)
    config = load_user_config(workspace=workspace)
    index = store.load_index().strip() or "(empty MEMORY.md)"
    topics = store.list_topics()
    latest_session = get_latest_session(workspace)
    sessions = list_sessions(workspace)
    trace_root = workspace / ".mokioclaw" / "executions"
    traces = []
    if trace_root.exists():
        for path in sorted(trace_root.iterdir(), reverse=True):
            summary_path = path / "summary.json"
            if not summary_path.exists():
                continue
            try:
                import json
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    traces.append(data)
            except Exception:
                continue
    topic_lines = [f"- {t.name} ({t.topic_type}): {t.description}" for t in topics[:10]] or ["(no topics)"]
    config_lines = [f"- {source}" for source in config.config_sources] or ["(no config sources)"]
    session_lines = [
        f"- {item.get('session_id')} [{item.get('status', 'unknown')}] turn={item.get('turn_index', 0)}"
        for item in sessions[:5]
    ] or ["(no sessions)"]
    trace_lines = [
        f"- {item.get('trace_id')} [{item.get('status', 'unknown')}] {item.get('summary', '')}"
        for item in traces[:5]
    ] or ["(no traces)"]
    memory_root = project_memory_dir(workspace)
    lines = [
        f"workspace: {workspace}",
        f"memory dir: {store.memory_dir}",
        f"memory root: {memory_root}",
        f"topics: {len(topics)}",
        f"sessions: {len(sessions)}",
        f"traces: {len(traces)}",
        f"config sources: {len(config.config_sources)}",
        "",
        "Config Sources:",
        *config_lines,
        "",
        "Latest Session:",
        f"- {latest_session.get('session_id') if latest_session else '(none)'}",
        f"- status: {latest_session.get('status') if latest_session else '(none)'}",
        f"- task: {(latest_session.get('task') if latest_session else '')[:160]}",
        "",
        "Recent Sessions:",
        *session_lines,
        "",
        "Recent Traces:",
        *trace_lines,
        "",
        "Index:",
        index[:2000],
        "",
        "Topics:",
        *topic_lines,
    ]
    sections = [
        ("Config Sources", config_lines),
        ("Latest Session", [
            f"- {latest_session.get('session_id') if latest_session else '(none)'}",
            f"- status: {latest_session.get('status') if latest_session else '(none)'}",
            f"- task: {(latest_session.get('task') if latest_session else '')[:160]}",
        ]),
        ("Recent Sessions", session_lines),
        ("Recent Traces", trace_lines),
        ("Index", [index[:2000]]),
        ("Topics", topic_lines),
    ]
    return CommandResult(kind=CommandKind.SYSTEM, name="memory", ui_message=_card_sections("Memory", [f"workspace: {workspace}", f"memory dir: {store.memory_dir}", f"memory root: {memory_root}", f"topics: {len(topics)}", f"sessions: {len(sessions)}", f"traces: {len(traces)}", f"config sources: {len(config.config_sources)}"], sections), action="none")


def _discover_all_skills(workspace: Path | None) -> list[Skill]:
    skills: list[Skill] = []
    if _DISABLE_BUNDLED:
        return []
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
    # 名字直接拼路径：../x 可读工作区命令目录之外的 .md，先校验字符集
    import re as _re

    if not _re.match(r"^[A-Za-z0-9_\-一-鿿]+$", name or ""):
        return None
    path = workspace / ".mokioclaw" / "commands" / f"{name}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Failed to read custom command %s: %s", path, exc)
        return None


def _resume_command(args: str, workspace: Path | None) -> CommandResult:
    """处理 /resume 命令

    /resume - 恢复最新 session
    /resume <sessionId> - 恢复指定 session
    """
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import build_resume_context, get_latest_session, load_session

    ws = workspace or default_workspace()
    session_id = args.strip() if args else None

    if session_id:
        session_data = load_session(ws, session_id)
        if not session_data:
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name="resume",
                ui_message=_card("Resume", [f"Session not found: {session_id}", f"workspace: {ws}"]),
                action="none",
            )
    else:
        session_data = get_latest_session(ws)
        if not session_data:
            return CommandResult(
                kind=CommandKind.SYSTEM,
                name="resume",
                ui_message=_card("Resume", ["No session to resume.", f"workspace: {ws}"]),
                action="none",
            )
        session_id = session_data["session_id"]

    resume_context = build_resume_context(session_data)
    last_state = session_data.get("last_state_summary") or {}
    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="resume",
        ui_message=_card_sections(
            "Resume",
            [f"session: {session_id}", f"turns: {session_data.get('turn_index', 0)}", f"status: {session_data.get('status', 'unknown')}", f"latest checkpoint: {session_data.get('latest_checkpoint') or '(none)'}"],
            [("Task", [session_data.get('task', '')[:120] or "(none)"]), ("Continue", [str(last_state.get('repair_instruction') or last_state.get('plan_summary') or session_data.get('task', ''))[:160]])],
        ),
        action="resume",
        meta={"session_id": session_id, "resume_context": resume_context, "session_data": session_data},
    )


def _rollback_command(args: str, workspace: Path | None) -> CommandResult:
    """处理 /rollback 命令

    /rollback <turn> - 回滚到指定轮次
    """
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import get_latest_session, rollback_to_turn

    ws = workspace or default_workspace()

    if not args:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="rollback",
            ui_message="Usage: /rollback <turn_number>",
            action="none",
        )

    try:
        turn = int(args.strip())
    except ValueError:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="rollback",
            ui_message=f"Invalid turn number: {args}",
            action="none",
        )

    session_data = get_latest_session(ws)
    if not session_data:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="rollback",
            ui_message="No session to rollback.",
            action="none",
        )

    session_id = session_data["session_id"]
    checkpoint = rollback_to_turn(ws, session_id, turn)
    if not checkpoint:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="rollback",
            ui_message=f"Failed to rollback to turn {turn}.",
            action="none",
        )

    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="rollback",
        ui_message=f"Rolled back to turn {turn}.",
        action="rollback",
        meta={"session_id": session_id, "turn": turn},
    )


def _sessions_command(workspace: Path | None) -> CommandResult:
    """处理 /sessions 命令 -列出所有 session"""
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import list_sessions

    ws = workspace or default_workspace()
    sessions = list_sessions(ws)

    if not sessions:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="sessions",
            ui_message="No sessions found.",
            action="none",
        )

    lines = ["Sessions:"]
    for s in sessions[:10]:  # 最多显示 10 个
        status = s.get("status", "unknown")
        turn = s.get("turn_index", 0)
        task = (s.get("task", "") or "")[:50]
        lines.append(f"  {s['session_id']}  turn={turn}  status={status}")
        if task:
            lines.append(f"    task: {task}")

    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="sessions",
        ui_message="\n".join(lines),
        action="none",
        meta={"sessions": sessions},
    )




def _cost_command(workspace: Path | None) -> CommandResult:
    """Show token usage and estimated cost for the current session."""
    import json
    if workspace is None:
        return CommandResult(kind=CommandKind.SYSTEM, name="cost", ui_message=_card("Cost", ["No workspace available."]), action="none")
    trace_root = workspace / ".mokioclaw" / "executions"
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_cost = 0.0
    tool_calls = 0
    trace_count = 0
    latest_cost = 0.0
    latest_tokens = 0
    latest_name = ""
    per_model: dict[str, dict[str, float]] = {}
    if trace_root.exists():
        trace_dirs = sorted(trace_root.iterdir(), reverse=True)
        for idx, trace_dir in enumerate(trace_dirs):
            summary_path = trace_dir / "summary.json"
            if not summary_path.exists():
                continue
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    trace_count += 1
                    p = int(data.get("prompt_tokens", 0))
                    c = int(data.get("completion_tokens", 0))
                    total_prompt += p
                    total_completion += c
                    total_tokens += int(data.get("total_tokens", 0)) or (p + c)
                    tool_calls += int(data.get("tool_calls", 0))
                    # 新 summary 直接有 cost_usd；旧 summary 按 token 估算
                    cost = float(data.get("cost_usd", 0) or 0)
                    if cost <= 0 and (p or c):
                        from mokioclaw.reliability.cost import estimate_cost_usd
                        cost = estimate_cost_usd(str(data.get("model", "")), p, c)
                    total_cost += cost
                    if idx == 0:
                        latest_cost = cost
                        latest_tokens = int(data.get("total_tokens", 0)) or (p + c)
                        latest_name = str(data.get("model", ""))
                    model = str(data.get("model", "") or "(unknown)")
                    entry = per_model.setdefault(model, {"prompt": 0, "completion": 0, "cost": 0.0})
                    entry["prompt"] += p
                    entry["completion"] += c
                    entry["cost"] += cost
            except Exception:
                continue
    lines = [
        f"workspace: {workspace}",
        f"traces: {trace_count}",
        f"latest trace: {latest_tokens:,} tokens, ${latest_cost:.4f}" + (f" ({latest_name})" if latest_name else ""),
        f"session total prompt tokens: {total_prompt:,}",
        f"session total completion tokens: {total_completion:,}",
        f"session total tokens: {total_tokens:,}",
        f"session total cost (est.): ${total_cost:.4f}",
        f"tool calls: {tool_calls}",
    ]
    if len(per_model) > 1:
        lines.append("per model:")
        for model, entry in sorted(per_model.items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(f"  {model}: {int(entry['prompt'] + entry['completion']):,} tokens, ${entry['cost']:.4f}")
    return CommandResult(kind=CommandKind.SYSTEM, name="cost", ui_message=_card("Cost", lines), action="none")


def _model_command(args: str, workspace: Path | None) -> CommandResult:
    """Show or switch the active model at runtime.

    /model            Show current model and override status
    /model <name>     Switch to <name> (takes effect next turn)
    /model reset      Clear the override, fall back to env default
    """
    import os
    from mokioclaw.providers.openai_provider import get_active_model

    env_model = os.getenv("MOKIO_MODEL_NAME", "") or os.getenv("MODEL", "") or os.getenv("OPENAI_MODEL", "") or "(default)"
    override = get_active_model()
    model_arg = (args or "").strip()
    if not model_arg:
        lines = [
            f"current: {override or env_model}",
            f"provider: {os.getenv('MOKIO_MODEL_PROVIDER', '') or os.getenv('BASE_URL', '') or 'openai'}",
            f"override: {override or '(none, using env default)'}",
            "usage: /model <name> | /model reset",
        ]
        return CommandResult(kind=CommandKind.SYSTEM, name="model", ui_message=_card("Model", lines), action="none")
    # reset：清除覆盖文件与进程内覆盖，回落 env 默认
    if model_arg.lower() in {"reset", "default", "clear"}:
        if workspace is not None:
            try:
                path = workspace / ".mokioclaw" / "model_override"
                path.unlink(missing_ok=True)
            except OSError:
                pass
        from mokioclaw.providers.openai_provider import set_active_model
        set_active_model(None)
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="model",
            ui_message=_card("Model", [f"cleared override: {override or '(none)'}", f"restored env default: {env_model}"]),
            action="model_switch",
            meta={"model": ""},
        )
    # Persist to workspace so create_runtime picks it up
    if workspace is not None:
        try:
            path = workspace / ".mokioclaw" / "model_override"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(model_arg, encoding="utf-8")
        except OSError:
            pass
    from mokioclaw.providers.openai_provider import set_active_model
    set_active_model(model_arg)
    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="model",
        ui_message=_card("Model", [f"previous: {override or env_model}", f"switched to: {model_arg}", "Takes effect on next turn."]),
        action="model_switch",
        meta={"model": model_arg},
    )


def _permissions_command(args: str, workspace: Path | None) -> CommandResult:
    if workspace is None:
        return CommandResult(kind=CommandKind.SYSTEM, name="permissions", ui_message=_card("Permissions", ["No workspace available."]), action="none")
    from mokioclaw.config.loader import load_user_config

    parts = (args or "").split()
    sub = parts[0].lower() if parts else "list"
    rest = parts[1:]

    config = load_user_config(workspace=workspace)
    allowed = list(config.allowed_tools)
    disallowed = list(config.disallowed_tools)

    if sub in {"list", "show", ""}:
        return _permissions_view(workspace, config, allowed, disallowed)

    if sub == "add" and rest:
        target = rest[0].lower()
        if target not in {"allow", "deny"}:
            return _permissions_usage()
        for rule in rest[1:]:
            if rule not in allowed and target == "allow":
                allowed.append(rule)
            if rule not in disallowed and target == "deny":
                disallowed.append(rule)
        _save_permissions(workspace, allowed, disallowed)
        return _permissions_view(workspace, config, allowed, disallowed)

    if sub == "remove" and rest:
        target = rest[0].lower()
        if target not in {"allow", "deny"}:
            return _permissions_usage()
        if target == "allow":
            allowed = [r for r in allowed if r not in rest[1:]]
        else:
            disallowed = [r for r in disallowed if r not in rest[1:]]
        _save_permissions(workspace, allowed, disallowed)
        return _permissions_view(workspace, config, allowed, disallowed)

    if sub == "reset":
        _save_permissions(workspace, [], [])
        config = load_user_config(workspace=workspace)
        return _permissions_view(workspace, config, [], [])

    return _permissions_usage()


def _permissions_usage() -> CommandResult:
    msg = _card("Permissions", [
        "Usage:",
        "  /permissions                          Show current rules",
        "  /permissions add allow <tool>        Allow a tool (supports wildcards like mcp__*)",
        "  /permissions add deny <tool>        Deny a tool",
        "  /permissions remove allow <tool>     Remove an allow rule",
        "  /permissions remove deny <tool>     Remove a deny rule",
        "  /permissions reset                   Clear all custom rules",
    ])
    return CommandResult(kind=CommandKind.SYSTEM, name="permissions", ui_message=msg, action="none")


def _permissions_view(workspace: Path, config, allowed: list[str], disallowed: list[str]) -> CommandResult:
    lines = [
        f"workspace: {workspace}",
        f"agent mode: {config.agent_mode}",
        f"approval mode: {config.approval_mode}",
        f"allowed tools: {len(allowed)}",
        f"disallowed tools: {len(disallowed)}",
    ]
    sections = [
        ("Allowed", allowed or ["(none)"]),
        ("Disallowed", disallowed or ["(none)"]),
        ("Tips", ["Wildcards supported: mcp__*", "Rules are persisted to .mokioclaw/permissions.json"]),
    ]
    return CommandResult(kind=CommandKind.SYSTEM, name="permissions", ui_message=_card_sections("Permissions", lines, sections), action="none")


def _permissions_file(workspace: Path) -> Path:
    return workspace / ".mokioclaw" / "permissions.json"


def _save_permissions(workspace: Path, allowed: list[str], disallowed: list[str]) -> None:
    import json
    path = _permissions_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"allowed_tools": allowed, "disallowed_tools": disallowed}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _status_command(workspace: Path | None) -> CommandResult:
    import os
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import get_latest_session, list_sessions
    from mokioclaw.config.loader import load_user_config

    ws = workspace or default_workspace()
    latest = get_latest_session(ws)
    sessions = list_sessions(ws)
    config = load_user_config(workspace=ws)
    latest_trace = None
    trace_root = ws / ".mokioclaw" / "executions"
    if trace_root.exists():
        for path in sorted(trace_root.iterdir(), reverse=True):
            if (path / "summary.json").exists():
                latest_trace = path.name
                break
    model_name = os.getenv("MOKIO_MODEL_NAME", "") or os.getenv("OPENAI_MODEL", "") or "(default)"
    model_provider = os.getenv("MOKIO_MODEL_PROVIDER", "") or os.getenv("OPENAI_API_BASE", "") or "openai"
    try:
        from mokioclaw.providers.openai_provider import get_active_model
        active = get_active_model()
        if active:
            model_name = f"{active} (/model override)"
    except Exception:
        pass
    account_name = os.getenv("MOKIO_ACCOUNT_NAME", "") or os.getenv("USER", "") or os.getenv("USERNAME", "") or "(unknown)"
    api_connected = bool(os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("MOKIO_API_KEY"))
    lines = [
        f"workspace: {ws}",
        f"sessions: {len(sessions)}",
        f"agent mode: {config.agent_mode}",
        f"approval mode: {config.approval_mode}",
        f"trace mode: {config.trace_mode}",
        f"checkpoint mode: {config.checkpoint_mode}",
    ]
    sections = [
        ("Model", [
            f"- name: {model_name}",
            f"- provider: {model_provider}",
        ]),
        ("Account", [
            f"- user: {account_name}",
            f"- api connected: {api_connected}",
        ]),
        ("Permissions", [
            f"- allowed tools: {len(config.allowed_tools)}",
            f"- disallowed tools: {len(config.disallowed_tools)}",
        ]),
    ]
    if latest:
        sections.append(("Latest Session", [
            f"- latest session: {latest.get('session_id', '')}",
            f"- status: {latest.get('status', 'unknown')}",
            f"- turns: {latest.get('turn_index', 0)}",
            f"- task: {(latest.get('task', '') or '')[:120]}",
            f"- updated: {latest.get('updated_at', '')}",
        ]))
    else:
        sections.append(("Latest Session", ["(none)"]))
    sections.append(("Latest Trace", [latest_trace or "(none)"]))
    sections.append(("Commands", ["/help /continue /resume /sessions /rollback /mode /memory /permissions /export /branch /cd /compact /cost"]))
    return CommandResult(kind=CommandKind.SYSTEM, name="status", ui_message=_card_sections("Status", lines, sections), action="none", meta={"latest_session": latest, "sessions": sessions})


def _review_command(args: str, workspace: Path | None) -> CommandResult:
    """Review git diff for the current project."""
    import subprocess as sp
    if workspace is None:
        return CommandResult(kind=CommandKind.SYSTEM, name="review", ui_message=_card("Review", ["No workspace available."]), action="none")
    try:
        diff_result = sp.run(
            ["git", "diff", "--stat"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        )
        diff_stat = diff_result.stdout.strip() or "(no changes)"
    except Exception as exc:
        diff_stat = f"git diff failed: {exc}"
    try:
        log_result = sp.run(
            ["git", "log", "--oneline", "-5"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        )
        recent_commits = log_result.stdout.strip() or "(none)"
    except Exception:
        recent_commits = "(none)"
    lines = [
        f"workspace: {workspace}",
        "",
        "Diff Stat:",
        diff_stat,
        "",
        "Recent Commits:",
        recent_commits,
        "",
        "Use the agent to review specific files or hunks.",
    ]
    return CommandResult(kind=CommandKind.SYSTEM, name="review", ui_message=_card("Review", lines), action="review", meta={"diff_stat": diff_stat})


def _init_command(args: str, workspace: Path | None) -> CommandResult:
    """Generate a CLAUDE.md / .mokioclaw/config.md template for the current project."""
    if workspace is None:
        return CommandResult(kind=CommandKind.SYSTEM, name="init", ui_message=_card("Init", ["No workspace available."]), action="none")

    config_path = workspace / ".mokioclaw" / "config.md"
    rules_dir = workspace / ".claude" / "rules"

    # Detect project type
    has_pyproject = (workspace / "pyproject.toml").exists()
    has_package_json = (workspace / "package.json").exists()
    has_cargo = (workspace / "Cargo.toml").exists()
    has_go_mod = (workspace / "go.mod").exists()

    project_type = "unknown"
    if has_pyproject:
        project_type = "python"
    elif has_package_json:
        project_type = "node"
    elif has_cargo:
        project_type = "rust"
    elif has_go_mod:
        project_type = "go"

    # Count source files
    import os
    py_count = sum(1 for _ in workspace.rglob("*.py") if ".venv" not in str(_) and ".mokioclaw" not in str(_)) if has_pyproject else 0
    js_count = (
        sum(1 for _ in workspace.rglob("*.js") if "node_modules" not in str(_))
        + sum(1 for _ in workspace.rglob("*.ts") if "node_modules" not in str(_))
    ) if has_package_json else 0

    template = f"""---
agent_mode: auto
checkpoint_mode: light
trace_mode: on
---

# Project: {workspace.name}

## Overview
- type: {project_type}
- workspace: {workspace}

## Conventions
- Use type hints in all Python code
- Write tests for new functions
- Keep functions under 50 lines
- Follow the existing code style

## Architecture
- Source files: {py_count} Python, {js_count} JS/TS
- Config: .mokioclaw/config.md (this file)
- Rules: .claude/rules/*.md (modular, per-file rules)

## Notes
- This file is auto-generated by /init
- Edit it to add project-specific instructions
- Use .claude/rules/ for scoped rules with globs frontmatter
"""

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(template, encoding="utf-8")
        msg_lines = [f"created: {config_path}", f"project type: {project_type}", f"python files: {py_count}", "Edit .mokioclaw/config.md to customize."]
    else:
        msg_lines = [f"already exists: {config_path}", "Edit it manually or delete first."]

    # Also create rules dir
    if not rules_dir.exists():
        rules_dir.mkdir(parents=True)
        (rules_dir / "example.md").write_text("# Example Rule\n\n---\nglobs: \"**/*.py\"\n---\n\nUse 4-space indentation in Python files.\n", encoding="utf-8")
        msg_lines.append(f"created: {rules_dir}/example.md")

    return CommandResult(kind=CommandKind.SYSTEM, name="init", ui_message=_card("Init", msg_lines), action="none")


def _batch_command(args: str, workspace: Path | None) -> CommandResult:
    """Execute multiple tasks in parallel or sequence.

    /batch task1 | task2 | task3    Run tasks in parallel
    /batch --seq task1 | task2      Run tasks sequentially
    """
    if not args.strip():
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="batch",
            ui_message=_card("Batch", ["Usage: /batch task1 | task2 | task3", "  --seq  Run sequentially (default: parallel)"]),
            action="none",
        )
    sequential = False
    raw = args.strip()
    if raw.startswith("--seq"):
        sequential = True
        raw = raw[len("--seq"):].strip()
    tasks = [t.strip() for t in raw.split("|") if t.strip()]
    if not tasks:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="batch",
            ui_message=_card("Batch", ["No tasks found.", "Usage: /batch task1 | task2 | task3"]),
            action="none",
        )
    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="batch",
        ui_message=_card("Batch", [
            f"mode: {'sequential' if sequential else 'parallel'}",
            f"tasks: {len(tasks)}",
            *[f"  {i+1}. {t[:80]}" for i, t in enumerate(tasks)],
        ]),
        action="batch",
        meta={"tasks": tasks, "sequential": sequential},
    )


def _loop_command(args: str, workspace: Path | None) -> CommandResult:
    """Repeat a prompt or command at fixed intervals.

    /loop <seconds> <prompt>   Repeat <prompt> every <seconds> seconds
    /loop stop                 Stop the active loop
    """
    parts = (args or "").split(None, 1)
    if not parts or parts[0].lower() == "stop":
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="loop",
            ui_message=_card("Loop", ["Loop stopped."]),
            action="loop_stop",
        )
    try:
        interval = int(parts[0])
    except ValueError:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="loop",
            ui_message=_card("Loop", ["Usage: /loop <seconds> <prompt>", "  /loop stop"]),
            action="none",
        )
    prompt = parts[1].strip() if len(parts) > 1 else ""
    if not prompt:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="loop",
            ui_message=_card("Loop", ["Usage: /loop <seconds> <prompt>", "  /loop stop"]),
            action="none",
        )
    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="loop",
        ui_message=_card("Loop", [f"interval: {interval}s", f"prompt: {prompt[:120]}", "Use /loop stop to cancel."]),
        action="loop_start",
        meta={"interval": interval, "prompt": prompt},
    )


def _cd_command(args: str, workspace: Path | None) -> CommandResult:
    """Switch the effective working directory for bash commands.

    /cd <path>   Change cwd to <path> (relative to workspace or absolute within workspace)
    /cd          Reset to workspace root
    """
    from mokioclaw.core.paths import default_workspace

    ws = workspace or default_workspace()
    target = (args or "").strip()

    if not target:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="cd",
            ui_message=_card("Cd", [f"workspace: {ws}", "cwd reset to workspace root", "Use /cd <path> to switch."]),
            action="cd",
            meta={"cwd": str(ws)},
        )

    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = (ws / candidate).resolve()
    else:
        candidate = candidate.resolve()

    try:
        candidate.relative_to(ws.resolve())
    except ValueError:
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="cd",
            ui_message=_card("Cd", [f"Path outside workspace: {candidate}", f"workspace: {ws}"]),
            action="none",
        )

    if not candidate.exists():
        return CommandResult(
            kind=CommandKind.SYSTEM,
            name="cd",
            ui_message=_card("Cd", [f"Path does not exist: {candidate}", f"workspace: {ws}"]),
            action="none",
        )

    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="cd",
        ui_message=_card("Cd", [f"cwd: {candidate}", f"workspace: {ws}"]),
        action="cd",
        meta={"cwd": str(candidate)},
    )


def _branch_command(args: str, workspace: Path | None) -> CommandResult:
    """Fork a session into a new branch.

    /branch                  Fork latest session
    /branch <sessionId>      Fork specified session
    /branch <sessionId> <task>  Fork with new task description
    """
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import fork_session, get_latest_session, load_session

    ws = workspace or default_workspace()
    parts = (args or "").split(None, 1)
    session_id = parts[0].strip() if parts else ""
    new_task = parts[1].strip() if len(parts) > 1 else ""

    if session_id:
        source = load_session(ws, session_id)
        if not source:
            return CommandResult(kind=CommandKind.SYSTEM, name="branch", ui_message=_card("Branch", [f"Session not found: {session_id}", f"workspace: {ws}"]), action="none")
    else:
        source = get_latest_session(ws)
        if not source:
            return CommandResult(kind=CommandKind.SYSTEM, name="branch", ui_message=_card("Branch", ["No session to branch.", f"workspace: {ws}"]), action="none")
        session_id = source["session_id"]

    forked = fork_session(ws, session_id, task=new_task)
    if not forked:
        return CommandResult(kind=CommandKind.SYSTEM, name="branch", ui_message=_card("Branch", [f"Failed to fork session {session_id}."]), action="none")

    new_sid = forked["session_id"]
    return CommandResult(
        kind=CommandKind.SYSTEM,
        name="branch",
        ui_message=_card_sections(
            "Branch",
            [f"forked from: {session_id}", f"new session: {new_sid}", f"turns: {forked.get('turn_index', 0)}", f"status: {forked.get('status', 'running')}"],
            [("Task", [forked.get('task', '')[:120] or "(inherited)"]), ("Continue", [f"Use /resume {new_sid} to continue this branch."])],
        ),
        action="branch",
        meta={"session_id": new_sid, "forked_from": session_id, "session_data": forked},
    )


def _export_command(args: str, workspace: Path | None) -> CommandResult:
    """Export current session as markdown transcript."""
    import json
    from datetime import datetime
    from mokioclaw.core.paths import default_workspace
    from mokioclaw.reliability.session_store import get_latest_session

    ws = workspace or default_workspace()
    parts = (args or "").split()
    fmt = parts[0].lower() if parts else "md"
    if fmt not in {"md", "markdown", "json"}:
        fmt = "md"

    session_data = get_latest_session(ws)
    if not session_data:
        return CommandResult(kind=CommandKind.SYSTEM, name="export", ui_message=_card("Export", ["No session to export."]), action="none")

    session_id = session_data.get("session_id", "session")
    turns = session_data.get("turns", [])
    task = session_data.get("task", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if fmt in {"json"}:
        export_path = ws / ".mokioclaw" / f"export-{session_id}-{stamp}.json"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(session_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        export_path = ws / ".mokioclaw" / f"export-{session_id}-{stamp}.md"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Session Export: {session_id}", "", f"- task: {task}", f"- status: {session_data.get('status', 'unknown')}", f"- turns: {session_data.get('turn_index', 0)}", f"- exported: {stamp}", "", "---", ""]
        for turn in turns:
            role = turn.get("role", "unknown")
            content = turn.get("summary") or turn.get("content", "")
            lines.append(f"## {role} (turn {turn.get('turn', '?')})")
            lines.append("")
            lines.append(str(content)[:2000])
            lines.append("")
        export_path.write_text("\n".join(lines), encoding="utf-8")

    return CommandResult(kind=CommandKind.SYSTEM, name="export", ui_message=_card("Export", [f"format: {fmt}", f"session: {session_id}", f"path: {export_path}"]), action="none", meta={"export_path": str(export_path)})


def _card(title: str, lines: list[str]) -> str:
    return "[" + title + "]\n" + "\n".join(lines)


def _card_sections(title: str, summary_lines: list[str], sections: list[tuple[str, list[str]]]) -> str:
    lines = list(summary_lines)
    for section_title, section_lines in sections:
        lines.extend(["", f"{section_title}:"])
        lines.extend(section_lines or ["(none)"])
    return _card(title, lines)
