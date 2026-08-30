"""
dot.coding.commands — 斜杠命令系统

所有 / 开头命令为本地终端指令：不进入 message 上下文、不消耗 token。
通过 CommandRegistry 注册和执行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class SlashResult:
    """斜杠命令执行结果"""
    kind: str = "message"  # message | toast | clear_screen | quit | prompt | none
    text: str = ""
    level: str = "info"  # info | warn | error
    # kind == "prompt" 时：text 是要送入 agent 的完整 prompt（skill 内容 + 任务），
    # 由 UI 层（console / TUI）负责执行 agent 回合并落盘。


@dataclass
class SlashCommand:
    """单个斜杠命令"""
    name: str
    usage: str
    description: str
    handler: Callable[[str], SlashResult] = field(repr=False)


class CommandRegistry:
    """斜杠命令注册表"""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._host: Any = None  # CodingHost 引用，由 TUI/CLI 注入
        self._skill_command_names: set[str] = set()
        self._register_builtins()

    def set_host(self, host: Any) -> None:
        """注入 CodingHost 引用，使 /mode、/reload 等命令能操作实际状态"""
        self._host = host
        self.refresh_skills()

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

    # ============================================================
    # Skill 即命令（对标 Claude Code：/<skill-name> <task> 直接驱动 agent）
    # ============================================================

    def refresh_skills(self) -> int:
        """扫描 workspace 下的 SKILL.md，把每个 skill 注册为独立斜杠命令

        命令名 = skill 名；执行时展开 skill 内容 + 用户任务，以 kind="prompt"
        返回给 UI 层送入 agent。内置命令名冲突时跳过（内置优先）。
        """
        for name in list(self._skill_command_names):
            self._commands.pop(name, None)
        self._skill_command_names.clear()

        from .skills.manager import SkillManager

        workspace = getattr(self._host, "workspace", None) or Path.cwd()
        manager = SkillManager()
        try:
            manager.scan_directory(workspace)
        except OSError as exc:
            log.warning("[commands] skill scan failed: %s", exc)
            return 0

        registered = 0
        for skill in manager.list_skills():
            if skill.name in self._commands:
                log.warning("[commands] skill '%s' conflicts with existing command, skipped", skill.name)
                continue
            self.register(SlashCommand(
                name=skill.name,
                usage=f"/{skill.name} <task>",
                description=f"skill: {skill.description[:60]}",
                handler=self._make_skill_handler(skill),
            ))
            self._skill_command_names.add(skill.name)
            registered += 1
        if registered:
            log.info("[commands] %d skills registered as slash commands", registered)
        return registered

    @staticmethod
    def _make_skill_handler(skill) -> Callable[[str], SlashResult]:
        def handler(args: str) -> SlashResult:
            task = args.strip()
            prompt = skill.to_prompt_section()
            if task:
                prompt += f"\n\n<task>\n{task}\n</task>"
            return SlashResult(kind="prompt", text=prompt)
        return handler

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def list_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def execute(self, text: str) -> SlashResult:
        """执行斜杠命令"""
        parsed = self.parse(text)
        if parsed is None:
            return SlashResult(kind="toast", level="error", text="invalid command")
        name, args = parsed
        cmd = self._commands.get(name)
        if cmd is None:
            return SlashResult(
                kind="toast", level="error",
                text=f"unknown: /{name} (type /help for list)",
            )
        try:
            return cmd.handler(args)
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/{name} error: {exc}")

    def complete(self, text: str) -> list[str]:
        """Tab 补全"""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return []
        body = stripped[1:]
        if " " in body:
            return []
        prefix = body.lower()
        matches = [c for c in self._commands.values() if c.name.startswith(prefix)]
        if not matches:
            return []
        if len(matches) == 1:
            return [f"/{matches[0].name} "]
        return [f"/{c.name}" for c in matches]

    @staticmethod
    def parse(text: str) -> tuple[str, str] | None:
        """解析斜杠命令 → (command_name, args)"""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:]
        if not body:
            return None
        parts = body.split(None, 1)
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""

    def build_help_text(self) -> str:
        lines = ["[bold]slash commands[/bold]"]
        for c in self._commands.values():
            lines.append(f"  {c.usage:<28} {c.description}")
        return "\n".join(lines)

    def _register_builtins(self) -> None:
        """注册内置命令"""
        self.register(SlashCommand("help", "/help", "show help", self._cmd_help))
        self.register(SlashCommand("mode", "/mode [plan|edit|auto]", "switch agent mode", self._cmd_mode))
        self.register(SlashCommand("clear", "/clear", "clear screen", self._cmd_clear))
        self.register(SlashCommand("exit", "/exit", "exit", self._cmd_exit))
        self.register(SlashCommand("skill", "/skill <name>", "expand skill content", self._cmd_skill))
        self.register(SlashCommand("skilllist", "/skilllist", "list all skills", self._cmd_skilllist))
        self.register(SlashCommand("reload", "/reload", "reload extensions", self._cmd_reload))
        # DEAD: _cmd_compact 是空 stub，压缩子系统未接入
        self.register(SlashCommand("compact", "/compact", "compact context (L1/L2 now, L3 async)", self._cmd_compact))
        self.register(SlashCommand("sessions", "/sessions", "list saved sessions", self._cmd_sessions))
        self.register(SlashCommand("resume", "/resume <id>", "switch to a saved session", self._cmd_resume))
        self.register(SlashCommand("trace", "/trace [on|off]", "show or toggle tracing", self._cmd_trace))
        self.register(SlashCommand("rewind", "/rewind [n]", "rewind to turn n (messages + files)", self._cmd_rewind))

    def _cmd_help(self, args: str) -> SlashResult:
        return SlashResult(kind="message", text=self.build_help_text())

    def _cmd_mode(self, args: str) -> SlashResult:
        from .modes import AgentMode
        arg = args.strip()
        if not arg:
            return SlashResult(kind="toast", text="Usage: /mode [plan|edit|auto]")
        try:
            mode = AgentMode.from_str(arg)
            # 热切换：更新 host 实际状态，下一轮立即生效
            if self._host is not None:
                self._host.set_mode(mode)
            return SlashResult(kind="toast", level="info", text=f"switched to {mode.label} mode (hot)")
        except Exception:
            return SlashResult(kind="toast", level="error", text=f"invalid mode: {arg}")

    def _cmd_clear(self, args: str) -> SlashResult:
        return SlashResult(kind="clear_screen")

    def _cmd_exit(self, args: str) -> SlashResult:
        return SlashResult(kind="quit")

    def _cmd_skill(self, args: str) -> SlashResult:
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

            skill_dir = self._get_skill_dir()
            mgr = SkillManager()
            if skill_dir:
                mgr.scan_directory(skill_dir)
            expanded = mgr.expand_skill(skill_name)
            if expanded.startswith("Skill '") and "not found" in expanded:
                return SlashResult(kind="toast", level="error", text=expanded)

            # 拼接 skill 内容 + 用户指令，注入到 harness
            content = f"这个是一个{skill_name}的技能描述: {expanded}"
            log.info(f"注入 skill 内容: {content[:50]}")
            self._host._harness.append_message(UserMessage(content=content))

            # if self._host is not None and hasattr(self._host, "_harness") and self._host._harness is not None:
            #     self._host._harness.follow_up_message(
            #         UserMessage(content=content)
            #     )
            #     return SlashResult(kind="toast", level="info", text=f"Skill '{skill_name}' loaded")
            # DEAD: 旧架构残留，follow_up_message 机制已被 append_message 替代
            return SlashResult(kind="toast", level="warn", text="No active session to inject skill into")
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/skill error: {exc}")

    def _cmd_skilllist(self, args: str) -> SlashResult:
        """列出所有可用 skill"""
        try:
            from .skills.manager import SkillManager

            skill_dir = self._get_skill_dir()
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

    def _get_skill_dir(self) -> Path | None:
        """获取 skill 目录：优先 workspace/.dot/skills/，回退到 cwd"""
        if self._host is not None and hasattr(self._host, "workspace"):
            skill_dir = self._host.workspace / ".dot" / "skills"
            if skill_dir.is_dir():
                return skill_dir
        return Path.cwd()

    def _cmd_reload(self, args: str) -> SlashResult:
        """重载扩展（teardown 旧扩展 → 使旧 generation 失效 → 重新加载并同步 harness）"""
        try:
            if self._host is not None and hasattr(self._host, "refresh_extensions"):
                loaded = self._host.refresh_extensions()
                skills = self.refresh_skills()
                return SlashResult(kind="toast", level="info",
                                   text=f"extensions reloaded ({loaded} loaded, {skills} skills)")
            # 无 host 时退化为仅重新扫描（不推荐）
            from .extensions.runtime import ExtensionRuntime
            runtime = ExtensionRuntime()
            loaded = runtime.reload()
            return SlashResult(kind="toast", level="info", text=f"extensions reloaded ({loaded} loaded, no host)")
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/reload error: {exc}")

    def _cmd_sessions(self, args: str) -> SlashResult:
        """列出所有已保存会话"""
        if self._host is None:
            return SlashResult(kind="toast", level="error", text="/sessions requires host context")
        try:
            sessions = self._host.list_sessions()
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/sessions error: {exc}")
        if not sessions:
            return SlashResult(kind="message", text="no saved sessions")
        current = getattr(self._host, "session_id", None)
        lines = [f"  {sid}{' *' if sid == current else ''}  ({n} msgs, {ws})"
                 for s in sessions
                 for sid, n, ws in [(s.get("session_id", "?"),
                                     s.get("message_count", 0),
                                     s.get("workspace", "?"))]]
        return SlashResult(kind="message", text="sessions (* = current):\n" + "\n".join(lines))

    def _cmd_resume(self, args: str) -> SlashResult:
        """切换到指定历史会话"""
        sid = args.strip()
        if not sid:
            return SlashResult(kind="toast", level="warn", text="Usage: /resume <session_id>")
        if self._host is None:
            return SlashResult(kind="toast", level="error", text="/resume requires host context")
        if not self._host.resume_session(sid):
            return SlashResult(kind="toast", level="error", text=f"session not found: {sid}")
        count = len(self._host.session.messages) if hasattr(self._host, "session") else 0
        return SlashResult(kind="toast", level="info",
                           text=f"resumed {sid} ({count} messages restored)")

    def _cmd_trace(self, args: str) -> SlashResult:
        """查看/切换链路追踪"""
        arg = args.strip().lower()
        if arg in ("on", "off"):
            if self._host is None or not hasattr(self._host, "set_trace_enabled"):
                return SlashResult(kind="toast", level="error", text="/trace requires host context")
            self._host.set_trace_enabled(arg == "on")
        if self._host is None or not hasattr(self._host, "trace_info"):
            return SlashResult(kind="toast", level="error", text="/trace requires host context")
        info = self._host.trace_info()
        status = "on" if info["enabled"] else "off"
        return SlashResult(
            kind="message",
            text=f"tracing: {status}  (session {info['session_id']})\noutput: {info['output_dir']}",
        )

    def _cmd_rewind(self, args: str) -> SlashResult:
        """回滚对话与 workspace 文件到指定轮次"""
        arg = args.strip()
        if self._host is None or not hasattr(self._host, "rewind_to_turn"):
            return SlashResult(kind="toast", level="error", text="/rewind requires host context")

        if not arg:
            turns = self._host.list_turns()
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
            result = self._host.rewind_to_turn(turn_id)
        except ValueError:
            return SlashResult(kind="toast", level="error", text=f"unknown turn id: {turn_id}")
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/rewind error: {exc}")
        git_note = f", files -> {result['commit'][:8]}" if result["commit"] else " (no git snapshot)"
        return SlashResult(
            kind="toast", level="info",
            text=f"rewound to turn {turn_id}: {result['messages']} messages kept{git_note}",
        )

    def _cmd_compact(self, args: str) -> SlashResult:
        """触发上下文压缩（L1 截断可恢复结果 / L2 删老旧工具调用 / L3 调度 LLM 摘要）"""
        if self._host is None or not hasattr(self._host, "compact_context"):
            return SlashResult(kind="toast", level="error", text="/compact requires host context")
        try:
            report = self._host.compact_context()
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/compact error: {exc}")
        level = "warn" if report.startswith("no compaction") else "info"
        return SlashResult(kind="toast", level=level, text=report)


# 全局单例
_registry: CommandRegistry | None = None


# DEAD: _ext_generation / get_extension_generation()
# 旧架构遗留，代际管理已迁移到 ExtensionRuntime._generation
# _ext_generation: object | None = None  # ExtensionGeneration
#
#
# def get_extension_generation() -> object | None:
#     return _ext_generation


def get_command_registry() -> CommandRegistry:
    global _registry
    if _registry is None:
        _registry = CommandRegistry()
    return _registry


def get_extension_generation() -> object | None:
    return _ext_generation
