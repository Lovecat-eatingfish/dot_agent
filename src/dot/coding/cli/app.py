"""
dot.coding.cli.app — CLI 入口

使用 typer + rich 构建 CLI。
支持 agent / agent run / agent console 命令。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()

app = typer.Typer(
    name="agent",
    help="dot agent — Coding Agent CLI",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    mode: str = typer.Option("auto", "--mode", "-m"),
) -> None:
    """默认入口：启动 TUI 交互模式"""
    if ctx.invoked_subcommand is not None:
        return
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    _launch_tui(workspace_str, mode)


@app.command("run", help="一次性非交互任务")
def run_cmd(
    task: str = typer.Argument(None, help="任务文本"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    mode: str = typer.Option("auto", "--mode", "-m"),
) -> None:
    """一次性执行任务并输出结果"""
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())

    if not task:
        if not sys.stdin.isatty():
            task = sys.stdin.read().strip()
        if not task:
            console.print("[yellow]用法: agent run '你的问题'[/yellow]")
            raise typer.Exit(code=1)

    console.print(Panel(f"[bold]{task}[/bold]", title="dot agent run", border_style="cyan"))

    # TODO: 集成新的 workflow 引擎
    console.print("[yellow]⚠ 新架构 workflow 引擎待集成[/yellow]")


@app.command("console", help="控制台交互模式")
def console_cmd(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    mode: str = typer.Option("auto", "--mode", "-m"),
) -> None:
    """控制台交互模式（无 TUI）"""
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    console.print(f"[bold green]dot agent console[/bold green] workspace={workspace_str} mode={mode}")
    console.print("[yellow]⚠ 新架构 console 模式待集成[/yellow]")


def _launch_tui(workspace_str: str, mode: str) -> None:
    """启动 TUI 交互模式"""
    from dot.coding.cli.tui.app import DotTUI
    from dot.coding.modes import AgentMode

    workspace = Path(workspace_str)
    agent_mode = AgentMode.from_str(mode)
    tui = DotTUI(workspace=workspace, mode=agent_mode)
    tui.run()


def main() -> None:
    """入口点"""
    app()
