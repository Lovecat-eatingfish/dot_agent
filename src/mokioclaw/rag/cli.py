"""RAG CLI 子命令组

挂载到主 app：app.add_typer(rag_app, name="rag")
子命令：
- rag serve   启动 FastAPI RAG 服务
- rag stop    停止后台 RAG 服务
- rag status  查看 RAG 服务状态
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from typing import Annotated

from mokioclaw.core.log import get_logger, setup_logging
from mokioclaw.daemon.manager import DaemonManager

logger = get_logger(__name__)

rag_app = typer.Typer(help="RAG Web 服务：文档接入/分割/向量检索", no_args_is_help=True)

# RAG 服务专用 pid/log 文件名（避免与 daemon.pid 冲突）
_RAG_PIDFILE = "rag.pid"
_RAG_LOGFILE = "rag.log"


@rag_app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host", help="监听地址（默认仅本机；公网请配合 RAG_API_TOKEN）")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="监听端口")] = 8000,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace 目录（决定 .mokioclaw/rag 落盘位置）"),
    ] = None,
    foreground: Annotated[bool, typer.Option("--foreground", help="前台运行（默认后台 daemon）")] = False,
) -> None:
    """启动 RAG FastAPI 服务"""
    setup_logging()
    ws = workspace or Path.cwd()

    # 非 loopback 绑定且无 token 时告警（不强制阻断，避免破坏本地多网卡调试）
    import os
    import dotenv
    dotenv.load_dotenv()
    token = os.getenv("RAG_API_TOKEN", "").strip()
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        typer.echo(
            "WARNING: binding non-loopback without RAG_API_TOKEN — "
            "set RAG_API_TOKEN or use --host 127.0.0.1",
            err=True,
        )

    if foreground:
        _run_server(host, port, ws)
        return

    # 后台 daemon 模式：用 DaemonManager 管理 pidfile
    mgr = DaemonManager(workspace=ws, pidfile_name=_RAG_PIDFILE, log_file_name=_RAG_LOGFILE)
    if mgr.is_running():
        typer.echo(f"RAG service already running (pid {mgr._read_pid()})")
        raise typer.Exit(0)
    command = [
        sys.executable, "-m", "mokioclaw", "rag", "serve",
        "--host", host, "--port", str(port),
        "--workspace", str(ws),
        "--foreground",
    ]
    try:
        info = mgr.start(command)
        typer.echo(f"RAG service started: pid {info.pid}, http://{host}:{port}")
    except RuntimeError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1)


@rag_app.command("stop")
def stop(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace 目录"),
    ] = None,
) -> None:
    """停止后台 RAG 服务"""
    ws = workspace or Path.cwd()
    mgr = DaemonManager(workspace=ws, pidfile_name=_RAG_PIDFILE, log_file_name=_RAG_LOGFILE)
    if not mgr.is_running():
        typer.echo("RAG service is not running.")
        raise typer.Exit(0)
    if mgr.stop():
        typer.echo("RAG service stopped.")
    else:
        typer.echo("Failed to stop RAG service (try force).", err=True)
        raise typer.Exit(1)


@rag_app.command("status")
def status(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace 目录"),
    ] = None,
) -> None:
    """查看 RAG 服务状态"""
    ws = workspace or Path.cwd()
    mgr = DaemonManager(workspace=ws, pidfile_name=_RAG_PIDFILE, log_file_name=_RAG_LOGFILE)
    if not mgr.is_running():
        typer.echo("RAG service is not running.")
        raise typer.Exit(0)
    info = mgr.get_info()
    typer.echo(
        f"RAG service running: pid {info.pid}, status {info.status}, "
        f"uptime {info.uptime_seconds:.0f}s"
    )


def _run_server(host: str, port: int, workspace: Path) -> None:
    """前台运行 uvicorn"""
    import uvicorn

    # 把 workspace 传给 service（通过环境变量，service 内 paths 默认用 cwd）
    import os
    os.chdir(workspace)
    from mokioclaw.rag.service import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port)
