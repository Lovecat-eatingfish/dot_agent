"""
dot.coding.cli.tui.app — TUI 交互应用

基于 prompt_toolkit + rich 的终端 UI。
使用 TuiState + TuiEventAdapter 模式消费 AgentEvent。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from dot.agent.events import AgentEvent
from dot.coding.commands import CommandRegistry, SlashResult, get_command_registry
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode

from .adapter import TuiEventAdapter
from .state import ChatItem, TuiState

console = Console(force_terminal=False)


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
        self.commands.set_host(self.host)
        self._running = False

        # TUI 状态
        self.state = TuiState()
        self.adapter = TuiEventAdapter(self.state)

        # 共享 harness：多轮对话消息历史累积
        self._harness = self.host.create_harness(
            system="You are a helpful coding assistant.",
        )

        # prompt_toolkit history
        history_path = self.workspace / ".dot" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
        )

    def run(self) -> None:
        """启动 TUI — 纯异步循环，用 asyncio.run 驱动"""
        # 统一日志：stderr + .dot/logs/dot.log
        from dot.coding.logging_config import setup as setup_logging
        setup_logging(workspace=self.workspace, level="INFO")

        self._running = True

        console.print(Panel(
            "[bold green]dot agent TUI[/bold green]\n"
            f"workspace: {self.workspace}\n"
            f"mode: {self.mode.label}\n"
            "Type /help for commands, Ctrl+C to quit",
            border_style="green",
        ))

        try:
            asyncio.run(self._async_loop())
        except KeyboardInterrupt:
            console.print("\n[dim]bye[/dim]")

    async def _async_loop(self) -> None:
        """纯异步主循环 — prompt_async + agent 处理都在同一事件循环中"""
        with patch_stdout():
            while self._running:
                current_mode = self.host.mode
                try:
                    user_input = await self._session.prompt_async(
                        f"[{current_mode.label}] > ",
                    )
                except KeyboardInterrupt:
                    console.print("\n[dim]bye[/dim]")
                    break
                except EOFError:
                    console.print("\n[dim]bye[/dim]")
                    break

                user_input = user_input.strip()
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
                await self._run_agent_turn_async(user_input)

    async def _run_agent_turn_async(self, user_input: str) -> None:
        """执行一轮 Agent 对话（异步）"""
        # 设置权限审批
        self.host.permission.set_approval_handler(self._make_approval_handler())

        try:
            async for event in self._harness.prompt(user_input):
                self.adapter.apply(event)
                self._render_latest()
            self.host.save_session()
        except KeyboardInterrupt:
            self._harness.cancel()
            console.print("\n[bold red]x interrupted[/bold red]")
        except Exception as exc:
            console.print(f"\n[bold red]x error: {exc}[/bold red]")

        # 清空 assistant buffer
        self.state.assistant_buffer = ""

    def _render_latest(self) -> None:
        """渲染最新的状态变更"""
        items = self.state.items
        if not items:
            return

        item = items[-1]
        self._render_item(item)

    def _render_item(self, item: ChatItem) -> None:
        """渲染单个 ChatItem"""
        if item.role == "assistant":
            if item.text:
                console.print(Markdown(item.text))

        elif item.role == "thinking":
            if self.state.show_thinking and item.text:
                console.print(f"[dim italic]> {item.text[:200]}[/dim italic]")

        elif item.role == "tool":
            # 工具调用
            if item.tool_result_text is None:
                # 正在执行
                elapsed = ""
                if item.started_at:
                    import time
                    secs = int(time.monotonic() - item.started_at)
                    if secs > 0:
                        elapsed = f" ({secs}s)"
                status = item.update_text or ""
                console.print(f"[yellow]- {item.text}{elapsed}[/yellow]")
                if status:
                    console.print(f"[dim]  {status[:200]}[/dim]")
            else:
                # 已完成
                console.print(f"[yellow]- {item.text}[/yellow]")
                console.print(f"[dim]{item.tool_result_text}[/dim]")

        elif item.role == "error":
            console.print(f"[bold red]{item.text}[/bold red]")

        elif item.role == "status":
            console.print(f"[dim]{item.text}[/dim]")

    def _render_slash_result(self, result: SlashResult) -> None:
        if result.kind == "message":
            console.print(result.text)
        elif result.kind == "toast":
            style = {"error": "bold red", "warn": "bold yellow"}.get(result.level, "bold cyan")
            console.print(f"[{style}]{result.text}[/{style}]")
        elif result.kind == "clear_screen":
            console.clear()
            self.state.clear()
        elif result.kind == "quit":
            self._running = False

    def _make_approval_handler(self):
        def handler(info: dict) -> bool:
            console.print()
            console.print("[bold yellow]permission required[/bold yellow]")
            console.print(f"  tool: {info.get('tool_name', '')}")
            console.print(f"  mode: {info.get('agent_mode', '')}")
            console.print(f"  reason: {info.get('reason', '')}")
            console.print(f"  args: {str(info.get('args', {}))[:200]}")
            try:
                answer = input("  approve? Y/N: ").strip().lower()
                return answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False
        return handler
