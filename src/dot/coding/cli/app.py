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
from dot.coding.state import WorkflowContext

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
        mode: str = typer.Option("auto", "--mode", "-m"),
        resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a saved session by id"),
) -> None:
    """Default: console interactive mode"""
    if ctx.invoked_subcommand is not None:
        return
    workspace_str = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
    sys.exit(run_console(Path(workspace_str), mode=mode, session_id=resume))


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
    """Run a task and output results via logging"""
    import logging
    from dot.coding.logging_config import setup as setup_logging

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
    context = WorkflowContext(task=task)

    try:
        async def _collect():
            from dot.coding.workflow import run_workflow
            async for event in run_workflow(context, host):
                if hasattr(event, "value"):
                    logger.info("[phase] %s", event.value)
                elif hasattr(event, "message"):
                    logger.info("[agent] %s", str(event)[:200])

        asyncio.run(_collect())
    except Exception as exc:
        logger.error("Failed: %s", exc, exc_info=True)
        raise typer.Exit(code=1)

    logger.info("done")


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
