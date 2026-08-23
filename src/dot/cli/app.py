"""
命令入口层 — Typer 子命令（对齐设计文档 §11）

  agent interactive          启动完整 TUI 交互模式（主入口）
  agent run "xxx"            一次性非交互任务，适合脚本调用
  agent config show          查看全部环境配置（压缩阈值、模型配置）
  agent mcp list             调试查看 MCP 工具列表

无子命令时：
  agent                      进入 TUI 交互模式
  agent "写个快排"            等价 agent run "写个快排"
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..core.log import get_logger, setup_logging
from ..host.agent_host import AgentHost
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


# ============================================================
# 主回调：无子命令时进入 TUI 交互模式
# ============================================================

@app.callback(invoke_without_command=True)
def _main(
        ctx: typer.Context,
        workspace: Optional[str] = typer.Option(
            None, "--workspace", "-w", help="工作目录（默认当前目录）"
        ),
        mode: str = typer.Option(
            "agent", "--mode", "-m", help=f"运行模式: {'|'.join(RUN_MODES)}"
        ),
) -> None:
    """dot agent 终端交互模块（无子命令时启动 TUI 交互模式）"""
    if ctx.invoked_subcommand is not None:
        return
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    interactive(workspace=workspace_str, mode=mode)


# ============================================================
# interactive — TUI 交互模式（主入口）
# ============================================================

@app.command("interactive", help="启动完整 TUI 交互模式")
def interactive(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("agent", "--mode", "-m"),
) -> None:
    setup_logging()
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    host = _make_host(workspace_str)
    bridge = SessionBridge(host)
    bridge.set_mode(mode)
    # 延迟导入，避免非 TUI 路径加载 textual
    from .tui.app import DotTUI

    tui = DotTUI(bridge)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        _teardown(host)


# ============================================================
# run — 一次性非交互任务
# ============================================================

@app.command("console", help="控制台调试交互模式（input() 循环，保留旧版调试命令）")
def console(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("auto", "--mode", "-m", help=f"初始模式: plan|edit|auto"),
) -> None:
    setup_logging()
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    host = _make_host(workspace_str)
    from .console import run_console

    try:
        run_console(host, agent_mode=mode)
    except KeyboardInterrupt:
        pass
    finally:
        _teardown(host)


@app.command("run", help="一次性非交互任务（适合脚本调用）")
def run(
        task: str = typer.Argument(..., help="任务文本"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("agent", "--mode", "-m"),
) -> None:
    setup_logging()
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    host = _make_host(workspace_str)
    bridge = SessionBridge(host)
    bridge.set_mode(mode)
    _run_once_print(bridge, task)
    _teardown(host)


def _run_once_print(bridge: SessionBridge, task: str) -> None:
    """非交互执行：用 Rich 打印事件流（无 TUI）"""
    from rich.markdown import Markdown

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
                console.print(f"[dim]· node: {ev.get('node', '')}[/dim]")
            elif kind == "final":
                console.print(Panel(ev.get("answer", ""), title="final", border_style="green"))
            elif kind == "intervention":
                console.print(f"[bold yellow]⚠ 需人工介入: {ev.get('reason', '')}[/bold yellow]")
            elif kind == "cancelled":
                console.print("[bold red]✗ 已中断[/bold red]")
            elif kind == "error":
                console.print(f"[bold red]✗ {ev.get('text', '')}[/bold red]")
    except KeyboardInterrupt:
        bridge.cancel()
        console.print("\n[bold red]✗ 已中断[/bold red]")


# ============================================================
# config — 查看环境配置
# ============================================================

config_app = typer.Typer(help="环境配置调试")
app.add_typer(config_app, name="config")


@config_app.command("show", help="查看全部环境配置（压缩阈值、模型配置）")
def config_show() -> None:
    setup_logging()
    table = Table(title="dot agent 配置", show_header=True, header_style="bold cyan")
    table.add_column("项", style="cyan")
    table.add_column("值")

    # 模型 / API 配置
    model_keys = ["MODEL", "BASE_URL", "API_KEY", "CONTEXT_WINDOW",
                  "DOT_LOG_LEVEL", "DOT_TRACE_ENABLED", "TAVILY_API_KEY"]
    for k in model_keys:
        v = os.environ.get(k, "")
        if k == "API_KEY" and v:
            v = v[:6] + "***" + v[-4:] if len(v) > 12 else "***"
        table.add_row(k, v or "(未设置)")

    # 压缩阈值
    try:
        from ..compress.budget import ContextBudgetAllocator

        b = ContextBudgetAllocator()
        info = b.get_budget_info()
        table.add_row("--- 压缩 ---", "")
        table.add_row("context_window", str(info["context_window"]))
        table.add_row("compression_threshold", str(info["compression_threshold"]))
        table.add_row("L1/L2/L3 触发", f"{info['l1_threshold']}/{info['l2_threshold']}/{info['l3_threshold']}")
        table.add_row("压缩后硬上限(75%)", str(info["max_after_compression"]))
    except Exception as exc:
        table.add_row("compress", f"读取失败: {exc}")

    # 运行模式
    table.add_row("--- 运行模式 ---", "")
    table.add_row("modes", " | ".join(RUN_MODES))

    console.print(table)


# ============================================================
# mcp — 调试查看 MCP 工具
# ============================================================

mcp_app = typer.Typer(help="MCP 调试")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("list", help="查看所有 MCP 服务、可用工具列表")
def mcp_list(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
) -> None:
    setup_logging()
    host = _make_host(workspace)
    bridge = SessionBridge(host)
    console.print(Panel(bridge.mcp_list(), title="MCP tools", border_style="cyan"))
    _teardown(host)


# ============================================================
# Host 工厂 / 清理
# ============================================================

def _make_host(workspace: Optional[str] = None) -> AgentHost:
    ws = Path(workspace).expanduser() if workspace else Path.cwd()
    logger.info("[cli] AgentHost init, workspace=%s", ws)
    return AgentHost(workspace=ws)


def _teardown(host: AgentHost) -> None:
    try:
        host.shared.close()
    except Exception as exc:
        logger.debug("[cli] teardown: %s", exc)
