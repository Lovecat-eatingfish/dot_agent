"""
dot.coding.cli.app — CLI 入口

使用 typer 构建 CLI。日志由 centralized logging_config 统一配置。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer

from dot.coding.cli.console_app import run_console
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode

app = typer.Typer(
    name="agent",
    help="dot agent — Coding Agent CLI",
    no_args_is_help=False,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def _main(
        ctx: typer.Context,
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("plan", "--mode", "-m"),
        resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a saved session by id"),
) -> None:
    """Default: interactive console (REPL)"""
    if ctx.invoked_subcommand is not None:
        return
    # todo： 测试阶段优先用这个console，后面使用tui
    workspace_path = Path(workspace).expanduser() if workspace else Path.cwd()
    sys.exit(run_console(workspace_path, mode=mode, session_id=resume))


@app.command("console", help="Interactive console mode")
def console_cmd(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("auto", "--mode", "-m"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
        resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a saved session by id"),
) -> None:
    """Interactive console (REPL) with logging"""
    workspace_path = Path(workspace).expanduser() if workspace else Path.cwd()
    sys.exit(run_console(workspace_path, mode=mode, verbose=verbose, session_id=resume))


@app.command("run", help="Run a one-shot task")
def run_cmd(
        task: str = typer.Argument(None, help="Task text"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("auto", "--mode", "-m"),
) -> None:
    """Run a one-shot task through the plan → code → validate workflow"""
    import logging
    from dot.agent.events import (
        MessageEndEvent, ToolExecutionEndEvent, ToolExecutionStartEvent,
    )
    from dot.ai.types import AssistantMessage
    from dot.coding.logging_config import setup as setup_logging
    from dot.coding.workflow import create_context, get_state, run_workflow
    from dot.workflow import WorkflowNodeStartEvent

    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    setup_logging(workspace=Path(workspace_str), level="INFO")
    logger = logging.getLogger("cli")

    if not task:
        if not sys.stdin.isatty():
            task = sys.stdin.read().strip()
        if not task:
            logger.error("Usage: agent run 'your task'")
            raise typer.Exit(code=1)

    logger.info("[run] task: %s", task[:200])

    host = CodingHost(workspace=Path(workspace_str), mode=AgentMode.from_str(mode))

    async def _run_one_shot() -> int:
        await host.connect_mcp()
        context = create_context(task)
        logger.info("─── phase: plan ───")  # 起始节点（变更时事件流才会再发）

        async for event in run_workflow(context, host, ui_mode="console"):
            if isinstance(event, WorkflowNodeStartEvent):
                logger.info("─── phase: %s ───", event.node)
            elif isinstance(event, ToolExecutionStartEvent):
                logger.info("[tool] %s %s", event.tool_name, str(event.args)[:150])
            elif isinstance(event, ToolExecutionEndEvent):
                status = "FAIL" if event.is_error else "OK"
                detail = event.result.text[:150] if event.result else ""
                logger.info("[tool] %s [%s] %s", event.tool_name, status, detail)
            elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                text = event.message.text
                if text.strip():
                    for line in text.rstrip().splitlines():
                        logger.info("[ai] %s", line)

        # 会话落盘（增量 + git 快照）与链路追踪收尾
        host.end_turn()
        host.flush_trace()
        state = get_state(context)
        if state.validate_result and not state.validate_result.passed:
            logger.error("validation failed: %s", state.validate_result.message[:200])
            return 1
        return 0

    try:
        exit_code = asyncio.run(_run_one_shot())
    except KeyboardInterrupt:
        if host._harness is not None:
            host._harness.cancel()
        host.flush_trace()
        logger.warning("interrupted")
        raise typer.Exit(code=130)
    except Exception as exc:
        logger.error("Failed: %s", exc, exc_info=True)
        raise typer.Exit(code=1)

    logger.info("work flow 运行完毕")
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("tui", help="Interactive TUI mode (Textual)")
def tui_cmd(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
        mode: str = typer.Option("auto", "--mode", "-m"),
        resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a saved session by id"),
) -> None:
    """Full-screen TUI (Textual): transcript + autocomplete + permission modal"""
    from dot.coding.cli.tui import DotTUI

    workspace_path = Path(workspace).expanduser() if workspace else Path.cwd()
    tui = DotTUI(workspace_path, mode=AgentMode.from_str(mode), session_id=resume)
    tui.run()


@app.command("sessions", help="List saved sessions")
def sessions_cmd(
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
) -> None:
    """List all saved sessions from <workspace>/.dot/sessions"""
    from dot.coding.session.manager import SessionManager

    workspace_path = Path(workspace).expanduser() if workspace else Path.cwd()
    manager = SessionManager(
        sessions_root=workspace_path / ".dot" / "sessions",
        workspace=workspace_path,
    )
    sessions = manager.list_sessions()
    if not sessions:
        print("no saved sessions")
        return
    print(f"{'session id':<14} {'messages':>8}  workspace")
    for s in sessions:
        print(f"{s.get('session_id', '?'):<14} {s.get('message_count', 0):>8}  {s.get('workspace', '?')}")


def main() -> None:
    app()
