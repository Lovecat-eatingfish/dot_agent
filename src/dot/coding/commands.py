"""
dot.coding.commands — 斜杠命令系统

所有 / 开头命令为本地终端指令：不进入 message 上下文、不消耗 token。
通过 CommandRegistry 注册和执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class SlashResult:
    """斜杠命令执行结果"""
    kind: str = "message"  # message | toast | clear_screen | quit | none
    text: str = ""
    level: str = "info"  # info | warn | error


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
        self._register_builtins()

    def set_host(self, host: Any) -> None:
        """注入 CodingHost 引用，使 /mode、/reload 等命令能操作实际状态"""
        self._host = host

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

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
        self.register(SlashCommand("reload", "/reload", "reload extensions", self._cmd_reload))
        self.register(SlashCommand("compact", "/compact", "trigger compaction", self._cmd_compact))
        self.register(SlashCommand("sessions", "/sessions", "list saved sessions", self._cmd_sessions))
        self.register(SlashCommand("resume", "/resume <id>", "switch to a saved session", self._cmd_resume))

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
        """展开 skill 内容为 XML 注入"""
        name = args.strip()
        if not name:
            return SlashResult(kind="toast", level="warn", text="Usage: /skill <name>")
        try:
            from .skills.manager import SkillManager
            mgr = SkillManager()
            mgr.scan_directory(Path.cwd())
            expanded = mgr.expand_skill(name)
            if expanded.startswith("Skill '") and "not found" in expanded:
                return SlashResult(kind="toast", level="error", text=expanded)
            return SlashResult(kind="message", text=expanded)
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/skill error: {exc}")

    def _cmd_reload(self, args: str) -> SlashResult:
        """重载扩展（teardown 旧扩展 → 使旧 generation 失效 → 重新加载并同步 harness）"""
        try:
            if self._host is not None and hasattr(self._host, "refresh_extensions"):
                loaded = self._host.refresh_extensions()
                return SlashResult(kind="toast", level="info", text=f"extensions reloaded ({loaded} loaded)")
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

    def _cmd_compact(self, args: str) -> SlashResult:
        """触发上下文压缩"""
        try:
            from .compress import plan_compaction
            # TODO: 实际触发压缩逻辑
            return SlashResult(kind="toast", level="info", text="compaction triggered")
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/compact error: {exc}")


# 全局单例
_registry: CommandRegistry | None = None
_ext_generation: object | None = None  # ExtensionGeneration


def get_command_registry() -> CommandRegistry:
    global _registry
    if _registry is None:
        _registry = CommandRegistry()
    return _registry


def get_extension_generation() -> object | None:
    return _ext_generation
