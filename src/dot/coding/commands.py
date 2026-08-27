"""
dot.coding.commands — 斜杠命令系统

所有 / 开头命令为本地终端指令：不进入 message 上下文、不消耗 token。
通过 CommandRegistry 注册和执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
        self._register_builtins()

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
            return SlashResult(kind="toast", level="error", text="无效命令")
        name, args = parsed
        cmd = self._commands.get(name)
        if cmd is None:
            return SlashResult(
                kind="toast", level="error",
                text=f"未知命令: /{name}（输入 /help 查看可用命令）",
            )
        try:
            return cmd.handler(args)
        except Exception as exc:
            return SlashResult(kind="toast", level="error", text=f"/{name} 执行出错: {exc}")

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
        lines = ["[bold]斜杠命令[/bold]"]
        for c in self._commands.values():
            lines.append(f"  {c.usage:<28} {c.description}")
        return "\n".join(lines)

    def _register_builtins(self) -> None:
        """注册内置命令"""
        self.register(SlashCommand("help", "/help", "展示帮助", self._cmd_help))
        self.register(SlashCommand("mode", "/mode [plan|edit|auto]", "查看/切换运行模式", self._cmd_mode))
        self.register(SlashCommand("clear", "/clear", "清空聊天界面", self._cmd_clear))
        self.register(SlashCommand("exit", "/exit", "退出", self._cmd_exit))

    def _cmd_help(self, args: str) -> SlashResult:
        return SlashResult(kind="message", text=self.build_help_text())

    def _cmd_mode(self, args: str) -> SlashResult:
        from .modes import AgentMode
        arg = args.strip()
        if not arg:
            return SlashResult(kind="toast", text="用法: /mode [plan|edit|auto]")
        try:
            mode = AgentMode.from_str(arg)
            return SlashResult(kind="toast", level="info", text=f"已切换到 {mode.label} 模式")
        except Exception:
            return SlashResult(kind="toast", level="error", text=f"无效模式: {arg}")

    def _cmd_clear(self, args: str) -> SlashResult:
        return SlashResult(kind="clear_screen")

    def _cmd_exit(self, args: str) -> SlashResult:
        return SlashResult(kind="quit")


# 全局单例
_registry: CommandRegistry | None = None


def get_command_registry() -> CommandRegistry:
    global _registry
    if _registry is None:
        _registry = CommandRegistry()
    return _registry
