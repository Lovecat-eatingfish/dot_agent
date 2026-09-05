"""
dot.coding.commands_builtin — 内置斜杠命令实现

每个 _cmd_* 函数职责单一：解析参数 → 操作 host/registry → 返回 SlashResult。
注册表（注册/分发/补全）在 dot.coding.commands.CommandRegistry，此处只放实现。
"""
from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .commands import SlashCommand, SlashResult

if TYPE_CHECKING:
    from .commands import CommandRegistry

log = logging.getLogger(__name__)


def register_builtin_commands(registry: "CommandRegistry") -> None:
    """把内置命令注册进注册表"""
    r = registry
    r.register(SlashCommand("help", "/help", "show help", partial(_cmd_help, r)))
    r.register(SlashCommand("mode", "/mode [plan|edit|auto]", "switch agent mode", partial(_cmd_mode, r)))
    r.register(SlashCommand("clear", "/clear", "clear screen", partial(_cmd_clear, r)))
    r.register(SlashCommand("exit", "/exit", "exit", partial(_cmd_exit, r)))
    r.register(SlashCommand("skill", "/skill <name>", "expand skill content", partial(_cmd_skill, r)))
    r.register(SlashCommand("skilllist", "/skilllist", "list all skills", partial(_cmd_skilllist, r)))
    r.register(SlashCommand("reload", "/reload", "reload extensions", partial(_cmd_reload, r)))
    # DEAD: _cmd_compact 是空 stub，压缩子系统未接入
    r.register(SlashCommand("compact", "/compact", "compact context (L1/L2 now, L3 async)", partial(_cmd_compact, r)))
    r.register(SlashCommand("sessions", "/sessions", "list saved sessions", partial(_cmd_sessions, r)))
    r.register(SlashCommand("resume", "/resume <id>", "switch to a saved session", partial(_cmd_resume, r)))
    r.register(SlashCommand("trace", "/trace [on|off]", "show or toggle tracing", partial(_cmd_trace, r)))
    r.register(SlashCommand("rewind", "/rewind [n]", "rewind to turn n (messages + files)", partial(_cmd_rewind, r)))
    r.register(SlashCommand("workflow", "/workflow <task>", "run plan → code → validate", partial(_cmd_workflow, r)))


def _cmd_help(registry: "CommandRegistry", args: str) -> SlashResult:
    return SlashResult(kind="message", text=registry.build_help_text())


def _cmd_workflow(registry: "CommandRegistry", args: str) -> SlashResult:
    task = args.strip()
    if not task:
        return SlashResult(kind="toast", level="warn", text="Usage: /workflow <task>")
    return SlashResult(kind="workflow", text=task)


def _cmd_mode(registry: "CommandRegistry", args: str) -> SlashResult:
    from .modes import AgentMode
    arg = args.strip()
    if not arg:
        return SlashResult(kind="toast", text="Usage: /mode [plan|edit|auto]")
    try:
        mode = AgentMode.from_str(arg)
        # 热切换：更新 host 实际状态，下一轮立即生效
        if registry._host is not None:
            registry._host.set_mode(mode)
        return SlashResult(kind="toast", level="info", text=f"switched to {mode.label} mode (hot)")
    except Exception:
        return SlashResult(kind="toast", level="error", text=f"invalid mode: {arg}")


def _cmd_clear(registry: "CommandRegistry", args: str) -> SlashResult:
    return SlashResult(kind="clear_screen")


def _cmd_exit(registry: "CommandRegistry", args: str) -> SlashResult:
    return SlashResult(kind="quit")


def _cmd_skill(registry: "CommandRegistry", args: str) -> SlashResult:
    """展开 skill 内容 + 用户指令，注入到 agent 对话"""
    parts = args.strip().split(None, 1)
    if not parts:
        return SlashResult(kind="toast", level="warn", text="Usage: /skill <name> [task]")
    skill_name = parts[0]
    user_task = parts[1] if len(parts) > 1 else ""
    log.info(f"展开 skill 内容: {skill_name}, task: {user_task[:50]}")
    try:
        from .skills.manager import SkillManager
        from dot.ai.types import UserMessage

        skill_dir = _get_skill_dir(registry)
        mgr = SkillManager()
        if skill_dir:
            mgr.scan_directory(skill_dir)
        expanded = mgr.expand_skill(skill_name)
        if expanded.startswith("Skill '") and "not found" in expanded:
            return SlashResult(kind="toast", level="error", text=expanded)

        # 拼接 skill 内容 + 用户指令，注入到 harness
        content = f"这个是一个{skill_name}的技能描述: {expanded}"
        log.info(f"注入 skill 内容: {content[:50]}")
        registry._host._harness.append_message(UserMessage(content=content))
        return SlashResult(kind="toast", level="warn", text="No active session to inject skill into")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/skill error: {exc}")


def _cmd_skilllist(registry: "CommandRegistry", args: str) -> SlashResult:
    """列出所有可用 skill"""
    try:
        from .skills.manager import SkillManager

        skill_dir = _get_skill_dir(registry)
        mgr = SkillManager()
        if skill_dir:
            mgr.scan_directory(skill_dir)
        skills = mgr.list_skills()
        if not skills:
            return SlashResult(kind="toast", level="warn", text="No skills found. Create SKILL.md files in .dot/skills/")
        lines = [f"[bold]Skills ({len(skills)})[/bold]"]
        for s in skills:
            lines.append(f"  /{s.name:<20} {s.description}")
        return SlashResult(kind="message", text="\n".join(lines))
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/skilllist error: {exc}")


def _get_skill_dir(registry: "CommandRegistry") -> Path | None:
    """获取 skill 目录：优先 workspace/.dot/skills/，回退到 cwd"""
    host: Any = registry._host
    if host is not None and hasattr(host, "workspace"):
        skill_dir = host.workspace / ".dot" / "skills"
        if skill_dir.is_dir():
            return skill_dir
    return Path.cwd()


def _cmd_reload(registry: "CommandRegistry", args: str) -> SlashResult:
    """重载扩展（teardown 旧扩展 → 使旧 generation 失效 → 重新加载并同步 harness）"""
    try:
        if registry._host is not None and hasattr(registry._host, "refresh_extensions"):
            loaded = registry._host.refresh_extensions()
            skills = registry.refresh_skills()
            return SlashResult(kind="toast", level="info",
                               text=f"extensions reloaded ({loaded} loaded, {skills} skills)")
        # 无 host 时退化为仅重新扫描（不推荐）
        from .extensions.runtime import ExtensionRuntime
        runtime = ExtensionRuntime()
        loaded = runtime.reload()
        return SlashResult(kind="toast", level="info", text=f"extensions reloaded ({loaded} loaded, no host)")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/reload error: {exc}")


def _cmd_sessions(registry: "CommandRegistry", args: str) -> SlashResult:
    """列出所有已保存会话"""
    if registry._host is None:
        return SlashResult(kind="toast", level="error", text="/sessions requires host context")
    try:
        sessions = registry._host.list_sessions()
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/sessions error: {exc}")
    if not sessions:
        return SlashResult(kind="message", text="no saved sessions")
    current = getattr(registry._host, "session_id", None)
    lines = [f"  {sid}{' *' if sid == current else ''}  ({n} msgs, {ws})"
             for s in sessions
             for sid, n, ws in [(s.get("session_id", "?"),
                                 s.get("message_count", 0),
                                 s.get("workspace", "?"))]]
    return SlashResult(kind="message", text="sessions (* = current):\n" + "\n".join(lines))


def _cmd_resume(registry: "CommandRegistry", args: str) -> SlashResult:
    """切换到指定历史会话"""
    sid = args.strip()
    if not sid:
        return SlashResult(kind="toast", level="warn", text="Usage: /resume <session_id>")
    if registry._host is None:
        return SlashResult(kind="toast", level="error", text="/resume requires host context")
    if not registry._host.resume_session(sid):
        return SlashResult(kind="toast", level="error", text=f"session not found: {sid}")
    count = len(registry._host.session.messages) if hasattr(registry._host, "session") else 0
    return SlashResult(kind="toast", level="info",
                       text=f"resumed {sid} ({count} messages restored)")


def _cmd_trace(registry: "CommandRegistry", args: str) -> SlashResult:
    """查看/切换链路追踪"""
    arg = args.strip().lower()
    if arg in ("on", "off"):
        if registry._host is None or not hasattr(registry._host, "set_trace_enabled"):
            return SlashResult(kind="toast", level="error", text="/trace requires host context")
        registry._host.set_trace_enabled(arg == "on")
    if registry._host is None or not hasattr(registry._host, "trace_info"):
        return SlashResult(kind="toast", level="error", text="/trace requires host context")
    info = registry._host.trace_info()
    status = "on" if info["enabled"] else "off"
    return SlashResult(
        kind="message",
        text=f"tracing: {status}  (session {info['session_id']})\noutput: {info['output_dir']}",
    )


def _cmd_rewind(registry: "CommandRegistry", args: str) -> SlashResult:
    """回滚对话与 workspace 文件到指定轮次"""
    arg = args.strip()
    if registry._host is None or not hasattr(registry._host, "rewind_to_turn"):
        return SlashResult(kind="toast", level="error", text="/rewind requires host context")

    if not arg:
        turns = registry._host.list_turns()
        if not turns:
            return SlashResult(kind="message", text="no turns recorded yet")
        lines = [f"  {t['turn_id']:>3}  {t['timestamp']}  {t['commit'][:8] or '-':<8}  {t['preview']}"
                 for t in turns]
        return SlashResult(kind="message", text="turns (latest last):\n" + "\n".join(lines))

    try:
        turn_id = int(arg)
    except ValueError:
        return SlashResult(kind="toast", level="error", text=f"invalid turn id: {arg}")
    try:
        result = registry._host.rewind_to_turn(turn_id)
    except ValueError:
        return SlashResult(kind="toast", level="error", text=f"unknown turn id: {turn_id}")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/rewind error: {exc}")
    git_note = f", files -> {result['commit'][:8]}" if result["commit"] else " (no git snapshot)"
    return SlashResult(
        kind="toast", level="info",
        text=f"rewound to turn {turn_id}: {result['messages']} messages kept{git_note}",
    )


def _cmd_compact(registry: "CommandRegistry", args: str) -> SlashResult:
    """触发上下文压缩（L1 截断可恢复结果 / L2 删老旧工具调用 / L3 调度 LLM 摘要）"""
    if registry._host is None or not hasattr(registry._host, "compact_context"):
        return SlashResult(kind="toast", level="error", text="/compact requires host context")
    try:
        report = registry._host.compact_context()
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"/compact error: {exc}")
    level = "warn" if report.startswith("no compaction") else "info"
    return SlashResult(kind="toast", level=level, text=report)
