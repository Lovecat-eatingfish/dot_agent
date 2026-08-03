"""
CLI 入口模块

基于 Typer 框架构建命令行接口，提供两种运行模式：

1. Rich 模式（默认）：mokioclaw "任务描述"
   - 单次执行，事件通过 Rich 渲染到终端
   - 适合快速任务和脚本调用

2. TUI 模式：mokioclaw tui
   - 基于 Textual 的终端 UI，支持多轮会话
   - 适合长时间交互式开发

Typer 框架要点：
- @app.callback(invoke_without_command=True) 定义"默认命令"
  当没有匹配的子命令时执行（如 mokioclaw "task"）
- @app.command("tui") 定义子命令（如 mokioclaw tui）
- Annotated[type, typer.Option(...)] 声明命令行选项
- typer.Argument(...) 声明位置参数
- typer.Context 传递运行上下文（如子命令信息）
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich import box
from rich.panel import Panel
from typer.core import TyperGroup

from mokioclaw.cli.formatter import print_event, safe_echo, safe_secho
from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.agent import stream_agent_events
from mokioclaw.core.log import get_logger, setup_logging

logger = get_logger(__name__)


class MokioClawGroup(TyperGroup):
    """自定义参数解析组，让 ``mokioclaw "task"`` 和子命令共存。

    默认 Typer 行为：未匹配的参数会报错。
    此类重写 parse_args，将非命令、非选项的内容收集为 task_arg。

    解析逻辑：
    1. 遇到已知子命令名或 --help → 剩余参数交给子命令处理
    2. 遇到 -开头的选项 → 当作选项处理（支持 --key=value 和 --key value）
    3. 其余所有内容 → 拼接为 task_arg 存入 ctx.obj
    """

    def parse_args(self, ctx, args):  # type: ignore[no-untyped-def]
        commands = set(self.commands)
        remaining: list[str] = []
        task_parts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            # 已知子命令或 --help，后面的内容交给子命令
            if arg in commands or arg == "--help":
                remaining.extend(args[index:])
                break
            # 选项（-开头），收集到 remaining
            if arg.startswith("-"):
                remaining.append(arg)
                # --key value 形式：下一个参数也属于这个选项
                if "=" not in arg and index + 1 < len(args) and not args[index + 1].startswith("-"):
                    remaining.append(args[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            # 非命令非选项 → 当作任务描述，后续全部作为任务内容
            task_parts.extend(args[index:])
            break
        if task_parts:
            ctx.obj = dict(ctx.obj or {})
            ctx.obj["task_arg"] = " ".join(task_parts)
        return super().parse_args(ctx, remaining)


# Typer 应用实例
# cls=MokioClawGroup 使用自定义参数解析
app = typer.Typer(
    cls=MokioClawGroup,
    help='mokioclaw: a teaching-first mini CodeAgent. Use `mokioclaw "task"` for Rich output or `mokioclaw tui` for Textual TUI.',
)


def configure_console() -> None:
    """配置标准输出/错误流为 UTF-8 编码。

    Windows 终端默认编码可能是 GBK，Rich/Textual 需要 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _install_signal_handlers() -> None:
    """安装信号处理器，确保 Ctrl+C 优雅退出"""
    if sys.platform == "win32":
        # Windows 不支持 SIGINT 的自定义 handler 通过 signal 模块，
        # 但 KeyboardInterrupt 仍然会被捕获
        return

    def _handle_sigint(signum, frame):
        raise KeyboardInterrupt()

    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt("SIGTERM received")

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigterm)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace for generated files. Defaults to a fresh .mokioclaw/workspaces/workspace-* directory."),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="Maximum planner/actor/verifier attempts before finalizing."),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="Human approval mode for high-risk BashTool commands: inline, auto, or deny."),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="Checkpoint mode: light, strict, or off."),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="Trace logging mode: on or off."),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="Resume from an existing MokioClaw workspace."),
    ] = None,
) -> None:
    """默认命令：执行 Agent 任务（Rich 终端输出）。

    当用户运行 mokioclaw "task" 时触发。
    如果调用了子命令（如 mokioclaw tui），则跳过此函数。

    执行流程：
    1. 检查是否调了子命令 → 是则跳过
    2. 从 ctx.obj 获取 MokioClawGroup 解析出的 task_arg
    3. 如果没有 task 也没有 resume → 显示 help
    4. 调用 stream_agent_events() 获取事件流
    5. 逐个事件通过 formatter.print_event() 渲染到终端
    """
    # 如果用户调了子命令（如 tui），callback 只做参数解析，不执行主逻辑
    if ctx.invoked_subcommand is not None:
        return
    setup_logging()
    from dotenv import load_dotenv
    load_dotenv()
    configure_console()
    _install_signal_handlers()
    task = None
    if isinstance(ctx.obj, dict):
        task = ctx.obj.get("task_arg")
    if not task and resume is None:
        safe_echo(ctx.get_help())
        raise typer.Exit()

    safe_secho("mokioclaw stage 5: MultiAgent + context/harness engineering", fg=typer.colors.MAGENTA)
    # inline 模式：危险命令时在终端弹出确认提示
    approval_handler = _inline_approval_handler if approval_mode == "inline" else None
    # stream_agent_events 是生成器，逐个 yield 事件字典
    try:
        for event in stream_agent_events(
            task,
            workspace=workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            approval_handler=approval_handler,
            checkpoint_mode=checkpoint_mode,
            resume_workspace=resume,
            trace_mode=trace_mode,
        ):
            print_event(event)
    except KeyboardInterrupt:
        safe_secho("\nInterrupted by user. Checkpoint saved.", fg=typer.colors.YELLOW)
        raise typer.Exit(130)
    except Exception as exc:
        logger.error("unexpected error: %s", exc, exc_info=True)
        safe_secho(f"\nFatal error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command("tui")
def tui(
    task: Annotated[str | None, typer.Argument(help="Optional initial task for the Textual TUI.")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace for the persistent TUI coding session."),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="Maximum planner/actor/verifier attempts before finalizing."),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="Human approval mode for high-risk BashTool commands: inline, auto, or deny."),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="Checkpoint mode: light, strict, or off."),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="Trace logging mode: on or off."),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="Resume from an existing MokioClaw workspace."),
    ] = None,
) -> None:
    """打开 Textual 终端 UI 界面。

    运行方式：mokioclaw tui [task]
    延迟导入 MokioClawTuiApp 避免启动 Rich 模式时加载 Textual 依赖。
    """
    configure_console()
    _install_signal_handlers()
    try:
        from mokioclaw.cli.tui import MokioClawTuiApp

        MokioClawTuiApp(
            initial_task=task,
            workspace=workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            checkpoint_mode=checkpoint_mode,
            trace_mode=trace_mode,
            resume=resume,
        ).run()
    except KeyboardInterrupt:
        safe_secho("\nTUI exited by user.", fg=typer.colors.YELLOW)
        raise typer.Exit(130)
    except Exception as exc:
        logger.error("TUI crashed: %s", exc, exc_info=True)
        safe_secho(f"\nTUI fatal error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)


def _inline_approval_handler(request: ApprovalRequest) -> ApprovalDecision:
    """Rich 模式的审批处理器。

    当 BashTool 检测到危险命令时调用：
    1. 用 Rich Panel 显示命令内容和风险原因
    2. 用 typer.prompt() 等待用户输入 y/N
    3. 返回 ApprovalDecision

    Args:
        request: 审批请求，包含命令、工具名、风险原因

    Returns:
        审批决定（通过/拒绝）
    """
    from mokioclaw.cli.formatter import console

    console.print(
        Panel(
            f"Command:\n{request.command}\n\nRisk:\n{request.risk_reason}",
            title=f"Human Approval · {request.tool_name}",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )
    answer = typer.prompt("Approve? [y/N]", default="n", show_default=False).strip().lower()
    console.print()
    approved = answer in {"y", "yes"}
    return ApprovalDecision(approved=approved, reason="" if approved else "Rejected by human operator.")
