"""
dot.coding.cli.tui.app — TUI 交互应用

基于 prompt_toolkit 的多轮对话终端 UI。
消费 AgentEvent 流，逐 token 渲染 assistant 输出。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from dot.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from dot.coding.commands import CommandRegistry, SlashResult, get_command_registry
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode

console = Console()


class DotTUI:
    """dot agent TUI 交互应用"""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        mode: AgentMode = AgentMode.AUTO,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.mode = mode
        self.host = CodingHost(workspace=self.workspace, mode=self.mode)
        self.commands = get_command_registry()
        self._running = False

        # prompt_toolkit history
        history_path = self.workspace / ".dot" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
        )

    def run(self) -> None:
        """启动 TUI 主循环"""
        self._running = True
        console.print(Panel(
            "[bold green]dot agent TUI[/bold green]\n"
            f"workspace: {self.workspace}\n"
            f"mode: {self.mode.label}\n"
            "输入 /help 查看命令，Ctrl+C 退出",
            border_style="green",
        ))

        with patch_stdout():
            while self._running:
                try:
                    user_input = self._session.prompt(
                        f"[{self.mode.label}] > ",
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]再见[/dim]")
                    break

                if not user_input:
                    continue

                # 斜杠命令
                if user_input.startswith("/"):
                    result = self.commands.execute(user_input)
                    self._render_slash_result(result)
                    if result.kind == "quit":
                        break
                    continue

                # 普通消息 → Agent
                self._run_agent_turn(user_input)

    def _run_agent_turn(self, user_input: str) -> None:
        """执行一轮 Agent 对话"""
        harness = self.host.create_harness(
            system="You are a helpful coding assistant.",
        )

        # 设置权限审批
        self.host.permission.set_approval_handler(self._make_approval_handler())

        # 收集 assistant 文本用于最终渲染
        assistant_text = ""
        tool_calls_info: list[str] = []

        try:
            for event in harness.prompt(user_input):
                assistant_text, tool_calls_info = self._handle_event(
                    event, assistant_text, tool_calls_info,
                )
        except KeyboardInterrupt:
            harness.cancel()
            console.print("\n[bold red]✗ 已中断[/bold red]")
        except Exception as exc:
            console.print(f"[bold red]✗ 错误: {exc}[/bold red]")

    def _handle_event(
        self,
        event: AgentEvent,
        assistant_text: str,
        tool_calls_info: list[str],
    ) -> tuple[str, list[str]]:
        """处理 Agent 事件，更新渲染状态"""
        if isinstance(event, AgentStartEvent):
            pass

        elif isinstance(event, TurnStartEvent):
            pass

        elif isinstance(event, MessageStartEvent):
            if hasattr(event.message, "role") and event.message.role == "user":
                console.print(f"[bold cyan]you>[/bold cyan] {event.message.text}")

        elif isinstance(event, MessageUpdateEvent):
            # 逐 token 渲染
            if hasattr(event.provider_event, "delta"):
                delta = event.provider_event.delta
                assistant_text += delta
                # 实时打印 delta（不换行）
                print(delta, end="", flush=True)

        elif isinstance(event, MessageEndEvent):
            if hasattr(event.message, "role"):
                if event.message.role == "assistant":
                    if assistant_text:
                        print()  # 换行
                        # 如果有完整文本，用 markdown 渲染
                        if len(assistant_text) > 50:
                            console.print(Markdown(assistant_text))
                    assistant_text = ""

        elif isinstance(event, ToolExecutionStartEvent):
            info = f"⚙ {event.tool_name}"
            tool_calls_info.append(info)
            console.print(f"[yellow]{info}[/yellow]")

        elif isinstance(event, ToolExecutionEndEvent):
            status = "✓" if not event.is_error else "✗"
            result_preview = event.result.text[:200] if event.result.text else ""
            console.print(f"[dim]  {status} {result_preview}[/dim]")

        elif isinstance(event, TurnEndEvent):
            pass

        elif isinstance(event, AgentEndEvent):
            console.print()  # 空行分隔

        return assistant_text, tool_calls_info

    def _render_slash_result(self, result: SlashResult) -> None:
        """渲染斜杠命令结果"""
        if result.kind == "message":
            console.print(result.text)
        elif result.kind == "toast":
            style = {"error": "bold red", "warn": "bold yellow"}.get(result.level, "bold cyan")
            console.print(f"[{style}]{result.text}[/{style}]")
        elif result.kind == "clear_screen":
            console.clear()
        elif result.kind == "quit":
            self._running = False

    def _make_approval_handler(self):
        """创建权限审批回调"""
        def handler(info: dict) -> bool:
            console.print()
            console.print("[bold yellow]【权限审批需人工确认】[/bold yellow]")
            console.print(f"  工具: {info.get('tool_name', '')}")
            console.print(f"  模式: {info.get('agent_mode', '')}")
            console.print(f"  原因: {info.get('reason', '')}")
            console.print(f"  参数: {str(info.get('args', {}))[:200]}")
            try:
                answer = input("  输入 Y 执行 / N 取消: ").strip().lower()
                return answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False
        return handler
