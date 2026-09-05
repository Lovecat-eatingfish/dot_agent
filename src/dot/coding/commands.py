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
    # kind == "workflow" 时：text 是 coding workflow 的任务文本。
    # 两者都由 UI 层（console / TUI）负责执行并落盘。


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

    @property
    def host(self) -> Any:
        """注入的 CodingHost 引用（可能为 None）"""
        return self._host

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
        """注册内置命令（实现在 commands_builtin 模块）"""
        from .commands_builtin import register_builtin_commands

        register_builtin_commands(self)


# 全局单例
_registry: CommandRegistry | None = None


def get_command_registry() -> CommandRegistry:
    global _registry
    if _registry is None:
        _registry = CommandRegistry()
    return _registry
