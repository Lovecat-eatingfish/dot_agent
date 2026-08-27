"""
命令入口层 — Typer 子命令

  agent                  启动 Textual TUI 交互模式（默认入口）
  agent run "xxx"        一次性非交互任务

无子命令时：agent 进入 TUI 交互模式
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from ..core.log import get_logger, setup_logging
from ..host.agent_host import AgentHost
from .config import CLIConfig
from .modes import RUN_MODES
from .session_bridge import SessionBridge

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    name="agent",
    help="dot agent — 终端交互模块",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback(invoke_without_command=True)
def _main(
        ctx: typer.Context,
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("agent", "--mode", "-m"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    _launch_tui(workspace_str, mode)


@app.command("run", help="一次性非交互任务")
def run_cmd(
        task: str = typer.Argument(None, help="任务文本（无参数时从 stdin 读取）"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("agent", "--mode", "-m"),
) -> None:
    setup_logging()
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())

    # 无参数时尝试从 stdin 读取
    if not task:
        if not sys.stdin.isatty():
            task = sys.stdin.read().strip()
        if not task:
            console.print("[yellow]用法: agent run \"你的问题\" 或 echo \"问题\" | agent run[/yellow]")
            raise typer.Exit(code=1)

    host = _make_host(workspace_str)
    if host is None:
        raise typer.Exit(code=1)
    bridge = SessionBridge(host)
    bridge.set_mode(mode)
    _run_once_print(bridge, task)
    _teardown(host)


def _launch_tui(workspace_str: str, mode: str) -> None:
    """启动 Textual TUI 交互模式"""
    setup_logging(quiet=True)

    # 加载配置并校验
    config = CLIConfig.load(workspace_str)
    warnings = config.validate()
    if warnings:
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")

    host = _make_host(workspace_str)
    if host is None:
        console.print("[red]AgentHost 初始化失败，请检查配置后重试。[/red]")
        return

    bridge = SessionBridge(host, config=config)
    bridge.set_mode(mode)

    from .tui.app import DotTUI

    tui = DotTUI(bridge)
    host.set_approval_handler(tui.make_approval_handler())
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        console.print(f"[red]TUI 运行错误：{exc}[/red]")
        raise
    finally:
        _teardown(host)


def _run_once_print(bridge: SessionBridge, task: str) -> None:
    """一次性执行并打印结果"""
    console.print(Panel(f"[bold]{task}[/bold]", title="dot agent run", border_style="cyan"))
    try:
        for ev in bridge.run_turn(task):
            kind = ev.get("kind", "")
            if kind == "user":
                console.print(f"[bold cyan]you>[/bold cyan] {ev.get('text', '')}")
            elif kind == "assistant":
                console.print(Markdown(ev.get("text", "")))
            elif kind == "tool_call":
                console.print(f"[yellow]⚙ {ev.get('name', '')}[/yellow] {ev.get('args', '')}")
            elif kind == "tool_result":
                console.print(f"[dim]  ↳ {ev.get('content', '')[:500]}[/dim]")
            elif kind == "node":
                console.print(f"[dim]· {ev.get('node', '')}[/dim]")
            elif kind == "final":
                answer = ev.get("answer", "") or ""
                summary = ev.get("text", "") or ""
                passed = ev.get("passed", False)
                if summary:
                    label = f"✓ PASSED — {summary}" if passed else (
                        f"■ STOPPED — {summary}" if "STOPPED" in answer
                        else f"✗ FAILED — {summary}"
                    )
                else:
                    label = "final"
                console.print(Panel(answer, title=label, border_style="green" if passed else "red"))
            elif kind == "intervention":
                console.print(f"[bold yellow]⚠ 需人工介入: {ev.get('reason', '')}[/bold yellow]")
            elif kind == "cancelled":
                console.print("[bold red]✗ 已中断[/bold red]")
            elif kind == "error":
                console.print(f"[bold red]✗ {ev.get('text', '')}[/bold red]")
    except KeyboardInterrupt:
        bridge.cancel()
        console.print("\n[bold red]✗ 已中断[/bold red]")


def _make_host(workspace: str | None = None) -> AgentHost | None:
    """创建 AgentHost，失败时返回 None（优雅降级）"""
    ws = Path(workspace).expanduser() if workspace else Path.cwd()
    try:
        logger.info("[cli] AgentHost init, workspace=%s", ws)
        return AgentHost(workspace=ws)
    except Exception as exc:
        logger.error("[cli] AgentHost init failed: %s", exc, exc_info=True)
        console.print(f"[red]AgentHost 初始化失败: {exc}[/red]")
        return None


def _teardown(host: AgentHost) -> None:
    try:
        host.shared.close()
    except Exception as exc:
        logger.debug("[cli] teardown: %s", exc)
