"""
CLI 入口模块
typer：命令行框架，类似argparse，写命令行工具超级省事；
    ctx = typer 上下文对象，用来在函数之间传递数据。ctx.obj是个字典，存自定义数据。

rich：终端美化，彩色输出、面板、表格；
textual：终端 GUI (TUI) 库，做终端窗口界面；

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




整个程序数据流（非常重要，帮你理清项目）

命令行输入 mokioclaw "写代码"
    ↓
MokioClawGroup.parse_args() 提取task_arg任务字符串
    ↓
main()回调函数
    ↓
stream_agent_events(任务,参数) 【core/agent.py，真正的Agent逻辑，生成器yield事件】
    ↓
循环拿到每一个event事件对象 → print_event()渲染到终端

这个 cli 文件只负责：命令解析、终端输出、信号处理、用户确认弹窗。
Agent 的 LLM 调用、工具调用、思考循环、plan‑actor‑verifier 逻辑全部在core/agent.py。
"""
from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich import box
from rich.panel import Panel
from typer.core import TyperGroup

from mokioclaw.interaction.formatter import print_event, safe_echo, safe_secho
from mokioclaw.core.utils import utc_now as _utc_now
from mokioclaw.security.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.orchestration.agent import stream_agent_events
from mokioclaw.core.log import get_logger, setup_logging
from mokioclaw.daemon.manager import DaemonManager
from mokioclaw.daemon.scheduler import get_scheduler, ScheduledTask, CronScheduler

logger = get_logger(__name__)


class MokioClawGroup(TyperGroup):
    """自定义参数解析组，让 ``mokioclaw "task"`` 和子命令共存。
    原因： 原生 Typer 有个限制：原生：mokioclaw "写代码"会报错，因为"写代码"不是子命令。
    我们想要效果：直接敲 mokioclaw "你的任务"，引号里面整串当做任务。

    example：mokioclaw -w ./work "帮我写快速排序"
        task_arg 就等于 "帮我写快速排序"，传给后面 main 函数。


    默认 Typer 行为：未匹配的参数会报错。
    此类重写 parse_args，将非命令、非选项的内容收集为 task_arg。

    解析逻辑：
    1. 遇到已知子命令名（tui）或 --help 交给typer 原生处理子命令 。 剩余参数交给子命令处理
    2. 遇到 -开头的选项  → 当作命令行选项处理（支持 --key=value 和 --key value）
    3. 其余所有内容 → 拼接为 task_arg 存入 ctx.obj
    """

    def parse_args(self, ctx, args):  # type: ignore[no-untyped-def]
        # 裸 --resume/--continue（后面无值）规范化为 =true，让"无参=恢复最新 session"可用
        args = [
            f"{arg}=true"
            if arg in ("--resume", "--continue")
            and (i + 1 >= len(args) or args[i + 1].startswith("-"))
            else arg
            for i, arg in enumerate(args)
        ]
        commands = set(self.commands)
        # 无值布尔标志：不能吞掉紧跟其后的任务字符串（否则 -p "task" 丢任务）
        _BOOL_FLAGS = {"-p", "--print", "--safe-mode", "--worktree", "--list-sessions"}
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
                # --key value 形式：下一个参数也属于这个选项（布尔标志除外）
                if (
                    "=" not in arg
                    and arg not in _BOOL_FLAGS
                    and not arg.startswith("--no-")
                    and index + 1 < len(args)
                    and not args[index + 1].startswith("-")
                ):
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

# 挂载 RAG 子命令组（mokioclaw rag serve/stop/status）
from mokioclaw.rag.cli import rag_app  # noqa: E402

app.add_typer(rag_app, name="rag")


def configure_console() -> None:
    """配置标准输出/错误流为 UTF-8 编码。
    Windows 终端默认编码 GBK，rich/textual需要 UTF‑8。
    修改 stdout/stderr 输出编码，防止中文乱码。


    Windows 终端默认编码可能是 GBK，Rich/Textual 需要 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _install_signal_handlers() -> None:
    """
    安装信号处理器，确保 Ctrl+C 优雅退出

    处理Ctrl+C：
        Linux/macOS 捕获SIGINT、SIGTERM信号，抛出KeyboardInterrupt；
        Windows 的 signal 模块不支持自定义信号处理器，直接跳过，系统依然会抛出 KeyboardInterrupt。
        作用：按Ctrl+C不会直接暴力杀掉进程，会优雅保存 checkpoint 检查点再退出
    """
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


def _find_workspace_for_resume(resume_value: str) -> Path | None:
    """--resume/--list-sessions/--rollback 未显式指定 -w 时定位已有 workspace

    默认 workspace 目录名带 uuid 后缀、每次运行都新建，直接在它下面查 session 永远为空。
    按 mtime 从新到旧扫描 workspaces 根目录：
    - resume_value 形如 session-xxx → 返回拥有该 session 的 workspace
    - resume_value 为空/"true"/None → 返回最近一个有 session 的 workspace
    - 其他（workspace 路径）→ 返回 None，交给调用方的路径分支处理
    """
    if resume_value and not resume_value.startswith("session-") and resume_value not in ("", "true"):
        return None
    from mokioclaw.core.paths import default_workspace_root
    from mokioclaw.reliability.session_store import get_latest_session, load_session

    root = default_workspace_root()
    if not root.exists():
        return None
    try:
        candidates = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    if resume_value.startswith("session-"):
        for ws in candidates:
            if load_session(ws, resume_value):
                return ws
        return None
    for ws in candidates:
        if get_latest_session(ws):
            return ws
    return None


def _format_resume_card(session: dict) -> str:
    """--resume 恢复前打印的 session 概要卡片（单行紧凑格式，配合 safe_secho 输出）"""
    task = str(session.get("task") or "").strip().replace("\n", " ")
    if len(task) > 60:
        task = task[:57] + "..."
    return (
        f"Resuming session {session.get('session_id', 'unknown')} | "
        f"turns: {session.get('turn_index', 0)} | "
        f"status: {session.get('status', 'unknown')} | "
        f"updated: {session.get('updated_at', '')} | "
        f"task: {task or '(none)'}"
    )


def _format_resume_context(event: dict) -> str:
    """session_resumed 自定义事件的终端展示文本（来自 stream_agent_events 的 custom_event）"""
    resume_context = str(event.get("resume_context") or "").strip()
    if len(resume_context) > 400:
        resume_context = resume_context[:397] + "..."
    lines = [
        f"Session resumed: {event.get('session_id', 'unknown')} "
        f"(turn {event.get('turn_index', 0)})",
    ]
    if resume_context:
        lines.append(f"Context: {resume_context}")
    return "\n".join(lines)


def _final_answer_from_graph_event(event: dict) -> str:
    """从 graph_event 提取最终答案（headless -p 输出用）

    两条路径：workflow 的 final 节点 final_answer；entry workflow 的
    chat_responder 节点 final_answer / chat_response（chat 路由提前返回）。
    """
    payload = event.get("event")
    if not isinstance(payload, dict):
        return ""
    for node in ("final", "chat_responder"):
        update = payload.get(node)
        if isinstance(update, dict):
            answer = str(update.get("final_answer") or update.get("chat_response") or "")
            if answer:
                return answer
    return ""


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
    agent_mode: Annotated[
        Literal["auto", "plan", "approve", "edit"],
        typer.Option("--agent-mode", help="Agent mode: auto, plan, approve, or edit."),
    ] = "auto",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="Checkpoint mode: light, strict, or off."),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="Trace logging mode: on or off."),
    ] = "on",
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Resume session. No arg=latest session, or sessionId (e.g. session-abc123)."),
    ] = None,
    continue_session: Annotated[
        str | None,
        typer.Option("--continue", help="Continue a session. Alias of --resume."),
    ] = None,
    safe_mode: Annotated[
        bool,
        typer.Option("--safe-mode", help="Clean start: disable all custom configs, hooks, and auto-memory."),
    ] = False,
    worktree: Annotated[
        bool,
        typer.Option("--worktree", help="Run in an isolated git worktree."),
    ] = False,
    list_sessions: Annotated[
        bool,
        typer.Option("--list-sessions", help="List all sessions and exit."),
    ] = False,
    rollback: Annotated[
        int | None,
        typer.Option("--rollback", help="Rollback to turn N in current session."),
    ] = None,
    print_mode: Annotated[
        bool,
        typer.Option("--print", "-p", help="Headless mode: run the task, print the result, exit. No interactive UI."),
    ] = False,
    output_format: Annotated[
        Literal["text", "json"],
        typer.Option("--output-format", help="Output format for -p headless mode: text or json."),
    ] = "text",
) -> None:
    """
    Rich 默认模式入口： @app.callback(invoke_without_command=True)：
        执行 mokioclaw tui → ctx.invoked_subcommand不为 None，直接 return 跳过 main 逻辑。
        执行 mokioclaw "任务" → 触发 main；


    工作空间， 最大重试次数， 批准模式， 检查点模式， 链路追踪模式，恢复对话
    默认命令：执行 Agent 任务（Rich 终端输出）。

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
    utc_stamp = _utc_now().replace(":", "-").replace("+", "").replace("T", "-")[:19]

    # 确定工作区
    from mokioclaw.core.paths import default_workspace
    # --continue 是 --resume 的别名
    resume_value = resume if resume is not None else continue_session
    resume_requested = resume_value is not None
    workspace_path = workspace or default_workspace()
    if workspace is None and (resume_requested or list_sessions or rollback is not None):
        # 未显式指定 -w：定位已有 workspace，否则每次都是新 uuid 目录，查不到任何 session
        located = _find_workspace_for_resume(resume_value or "")
        if located is not None:
            workspace_path = located
    workspace_path.mkdir(parents=True, exist_ok=True)

    # --list-sessions：列出所有 session
    if list_sessions:
        from mokioclaw.reliability.session_store import list_sessions as do_list_sessions
        sessions = do_list_sessions(workspace_path)
        if not sessions:
            safe_secho("No sessions found.", fg=typer.colors.YELLOW)
        else:
            safe_secho(f"Sessions in {workspace_path}:", fg=typer.colors.CYAN)
            for s in sessions:
                status = s.get("status", "unknown")
                turn = s.get("turn_index", 0)
                task_preview = (s.get("task", "") or "")[:60]
                updated = s.get("updated_at", "")
                safe_secho(
                    f"  {s['session_id']}  turn={turn}  status={status}  {updated}",
                    fg=typer.colors.GREEN,
                )
                if task_preview:
                    safe_secho(f"    task: {task_preview}", fg=typer.colors.WHITE)
        raise typer.Exit()

    # --rollback N：回滚到指定轮次
    if rollback is not None:
        from mokioclaw.reliability.session_store import (
            get_latest_session,
            rollback_to_turn,
        )
        session_data = get_latest_session(workspace_path)
        if not session_data:
            safe_secho("No session to rollback.", fg=typer.colors.RED)
            raise typer.Exit(1)
        session_id = session_data["session_id"]
        checkpoint = rollback_to_turn(workspace_path, session_id, rollback)
        if checkpoint:
            safe_secho(f"Rolled back session {session_id} to turn {rollback}.", fg=typer.colors.GREEN)
        else:
            safe_secho(f"Failed to rollback to turn {rollback}.", fg=typer.colors.RED)
        raise typer.Exit()

    if continue_session is not None and resume is None:
        resume = continue_session

    if worktree and not resume_requested:
        import subprocess as sp
        try:
            result = sp.run(
                ["git", "worktree", "add", "-q", str(workspace_path / ".mokioclaw" / "worktree-" + utc_stamp)],
                cwd=workspace_path, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                workspace_path = workspace_path / ".mokioclaw" / "worktree-" + utc_stamp
        except Exception:
            pass

    task = None
    if isinstance(ctx.obj, dict):
        task = ctx.obj.get("task_arg")

    # 如果既没有任务，也没有--resume恢复旧会话，打印帮助信息退出。
    if not task and not resume_requested:
        safe_echo(ctx.get_help())
        raise typer.Exit()

    # --output-format 只在 -p headless 模式下有意义，误用时提前提醒
    if output_format != "text" and not print_mode:
        safe_secho(f"--output-format is only used with -p (headless mode); ignoring {output_format}.", fg=typer.colors.YELLOW, err=True)

    if not print_mode:
        safe_secho("mokioclaw stage 5: MultiAgent + context/harness engineering", fg=typer.colors.MAGENTA)
    # inline 模式：危险命令时在终端弹出确认提示
    approval_handler = _inline_approval_handler if approval_mode == "inline" else None

    # --resume 处理：无参数恢复最新 session，有参数恢复指定 session / workspace 路径
    resume_session_id = None
    if resume_requested:
        if resume_value in ("", "true"):
            from mokioclaw.reliability.session_store import get_latest_session
            latest = get_latest_session(workspace_path)
            if latest:
                resume_session_id = latest["session_id"]
                safe_secho(_format_resume_card(latest), fg=typer.colors.CYAN)
            else:
                safe_secho("No session to resume.", fg=typer.colors.YELLOW)
                raise typer.Exit()
        elif resume_value.startswith("session-"):
            resume_session_id = resume_value
        else:
            workspace_path = Path(resume_value).expanduser()
            workspace_path.mkdir(parents=True, exist_ok=True)

    # stream_agent_events 是生成器，逐个 yield 事件字典
    # -p headless 模式（对齐 Claude Code -p / SDK）：只输出最终结果，无交互 UI
    if print_mode:
        final_answer = ""
        trace_summary: dict[str, Any] = {}
        session_id = ""
        try:
            for event in stream_agent_events(
                task,
                workspace=workspace_path,
                max_attempts=max_attempts,
                approval_mode=approval_mode,
                agent_mode=agent_mode,
                approval_handler=None,  # headless 无法人工审批
                checkpoint_mode=checkpoint_mode,
                resume_workspace=workspace_path if resume_requested else None,
                resume_session_id=resume_session_id,
                trace_mode=trace_mode,
                safe_mode=safe_mode,
            ):
                if event.get("type") == "graph_event":
                    final = _final_answer_from_graph_event(event)
                    if final:
                        final_answer = final
                elif event.get("type") == "custom_event" and isinstance(event.get("event"), dict):
                    evt = event["event"]
                    if evt.get("type") == "trace_summary":
                        trace_summary = dict(evt)
                    elif evt.get("type") == "session_finished":
                        session_id = str(evt.get("session_id", ""))
        except KeyboardInterrupt:
            safe_secho("Interrupted.", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(130)
        except Exception as exc:
            logger.error("headless run failed: %s", exc, exc_info=True)
            if output_format == "json":
                sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) + "\n")
            else:
                safe_secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        if output_format == "json":
            payload = {
                "ok": True,
                "final_answer": final_answer,
                "session_id": session_id,
                "trace_id": trace_summary.get("trace_id", ""),
                "prompt_tokens": trace_summary.get("prompt_tokens", 0),
                "completion_tokens": trace_summary.get("completion_tokens", 0),
                "total_tokens": trace_summary.get("total_tokens", 0),
                "cost_usd": trace_summary.get("cost_usd", 0),
                "tool_calls": trace_summary.get("tool_calls", 0),
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            if final_answer:
                safe_echo(final_answer)
            else:
                safe_secho("(no final answer produced)", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit()

    try:
        for event in stream_agent_events(
            task,
            workspace=workspace_path,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
            checkpoint_mode=checkpoint_mode,
            resume_workspace=workspace_path if resume_requested else None,
            resume_session_id=resume_session_id,
            trace_mode=trace_mode,
            safe_mode=safe_mode,
        ):
            # print_event(event)就是把事件美化打印到终端。
            if event.get("type") == "custom_event" and isinstance(event.get("event"), dict) and event["event"].get("type") == "session_resumed":
                safe_secho(_format_resume_context(event["event"]), fg=typer.colors.CYAN)
            print_event(event)
    except KeyboardInterrupt:
        # KeyboardInterrupt：用户Ctrl+C中断，提示检查点已保存，退出码 130；
        safe_secho("\nInterrupted by user. Session saved.", fg=typer.colors.YELLOW)
        raise typer.Exit(130)
    except Exception as exc:
        # 其他异常：打印致命错误，记录日志，退出码 1。
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
        str | None,
        typer.Option("--resume", help="Resume session. No arg=latest, or sessionId (e.g. session-abc123)."),
    ] = None,
    continue_session: Annotated[
        str | None,
        typer.Option("--continue", help="Continue a session. Alias of --resume."),
    ] = None,
    safe_mode: Annotated[
        bool,
        typer.Option("--safe-mode", help="Clean start: disable all custom configs, hooks, and auto-memory."),
    ] = False,
    worktree: Annotated[
        bool,
        typer.Option("--worktree", help="Run in an isolated git worktree."),
    ] = False,
) -> None:
    """
    运行命令：mokioclaw tui
    打开 Textual 终端 UI 界面。

    运行方式：mokioclaw tui [task]
    延迟导入 MokioClawTuiApp 避免启动 Rich 模式时加载 Textual 依赖。
    """
    configure_console()
    _install_signal_handlers()
    utc_stamp = _utc_now().replace(":", "-").replace("+", "-").replace("T", "-")[:19]
    # --continue 是 --resume 的别名
    if continue_session is not None and resume is None:
        resume = continue_session
    if worktree:
        safe_secho("--worktree is not supported in TUI mode yet, ignored.", fg=typer.colors.YELLOW)
    try:
        # 延迟导入 MokioClawTuiApp：只有跑 tui 子命令才导入 textual 相关代码；
        from mokioclaw.interaction.tui import MokioClawTuiApp
        MokioClawTuiApp(
            initial_task=task,
            workspace=workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            checkpoint_mode=checkpoint_mode,
            trace_mode=trace_mode,
            resume=resume,
            safe_mode=safe_mode,
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
    当 Agent 要执行高危 bash 命令时会调用这个函数：

    当 BashTool 检测到危险命令时调用：
    1. 用 Rich Panel 显示命令内容和风险原因
    2. 用 typer.prompt() 等待用户输入 y/N
    3. 返回 ApprovalDecision

    Args:
        request: 审批请求，包含命令、工具名、风险原因

    Returns:
        审批决定（通过/拒绝）
    """
    from mokioclaw.interaction.formatter import console

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


# ============================================================
# Daemon 子命令
# ============================================================

@app.command("daemon")
def daemon_status(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """查看后台 daemon 状态"""
    mgr = DaemonManager(workspace=workspace or Path.cwd())
    info = mgr.get_info()
    typer.echo(f"Status: {info.status}")
    if info.pid:
        typer.echo(f"PID: {info.pid}")
    if info.started_at:
        typer.echo(f"Started: {info.started_at}")
    if info.uptime_seconds > 0:
        typer.echo(f"Uptime: {_format_uptime(info.uptime_seconds)}")
    if info.status == "stopped":
        typer.echo("Daemon is not running.")


@app.command("serve")
def serve(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """daemon 后台进程入口：运行定时任务调度循环（由 daemon-start 拉起）"""
    import time as _time

    ws = workspace or Path.cwd()
    scheduler = CronScheduler(tasks_dir=ws / ".mokioclaw" / "tasks")
    scheduler.start()
    typer.echo(f"mokioclaw daemon serving {ws} (pid {__import__('os').getpid()})")
    try:
        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()


@app.command("daemon-start")
def daemon_start(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """启动后台 daemon"""
    ws = workspace or Path.cwd()
    mgr = DaemonManager(workspace=ws)
    if mgr.is_running():
        typer.echo(f"Daemon already running (pid {mgr._read_pid()})")
        raise typer.Exit(0)
    command = [sys.executable, "-m", "mokioclaw", "serve", "--workspace", str(ws)]
    try:
        info = mgr.start(command)
        typer.echo(f"Daemon started: pid {info.pid}")
    except RuntimeError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command("daemon-stop")
def daemon_stop(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """停止后台 daemon"""
    mgr = DaemonManager(workspace=workspace or Path.cwd())
    if mgr.stop():
        typer.echo("Daemon stopped.")
    else:
        typer.echo("Daemon was not running.")


# ============================================================
# Schedule 子命令
# ============================================================

@app.command("schedule")
def schedule_list(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """列出所有定时任务"""
    ws = workspace or Path.cwd()
    scheduler = CronScheduler(tasks_dir=ws / ".mokioclaw" / "tasks")
    tasks = scheduler.tasks
    if not tasks:
        typer.echo("No scheduled tasks.")
        return
    for task in tasks:
        typer.echo(f"[{task.id}] {task.name}")
        typer.echo(f"  Cron: {task.cron}")
        typer.echo(f"  Cmd:  {task.command} {' '.join(task.args)}")
        typer.echo(f"  Status: {task.status} | Runs: {task.run_count}")


@app.command("schedule-add")
def schedule_add(
    name: str,
    cron: str,
    command: str,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """添加定时任务

    Example: mokioclaw schedule-add "daily-report" "0 9 * * *" "echo hello"
    """
    ws = workspace or Path.cwd()
    scheduler = CronScheduler(tasks_dir=ws / ".mokioclaw" / "tasks")
    task = ScheduledTask(name=name, cron=cron, command=command)
    task_id = scheduler.add_task(task)
    typer.echo(f"Task added: {task_id}")


@app.command("schedule-remove")
def schedule_remove(
    task_id: str,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = None,
) -> None:
    """移除定时任务"""
    ws = workspace or Path.cwd()
    scheduler = CronScheduler(tasks_dir=ws / ".mokioclaw" / "tasks")
    if scheduler.remove_task(task_id):
        typer.echo(f"Task removed: {task_id}")
    else:
        typer.echo(f"Task not found: {task_id}")


# ============================================================
# MCP 子命令
# ============================================================

@app.command("mcp")
def mcp_list_servers() -> None:
    """列出已连接的 MCP Server"""
    from mokioclaw.mcp.bridge import get_mcp_bridge
    bridge = get_mcp_bridge()
    servers = bridge.list_servers()
    if not servers:
        typer.echo("No MCP servers connected.")
        return
    for name in servers:
        tools = bridge.list_tools(name)
        typer.echo(f"[{name}] {len(tools)} tools")
        for tool in tools[:10]:
            typer.echo(f"  - {tool.name}: {tool.description[:60]}")


@app.command("mcp-call")
def mcp_call_tool(
    tool_name: str = typer.Argument(help="Tool name in format 'server:tool'"),
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", "-a", help="Tool argument as key=value"),
    ] = None,
) -> None:
    """调用 MCP 工具

    Example: mokioclaw mcp-call fs:read_file -a path=/tmp/test.txt
    """
    from mokioclaw.mcp.bridge import get_mcp_bridge
    bridge = get_mcp_bridge()
    arguments: dict[str, Any] = {}
    if arg:
        for item in arg:
            if "=" in item:
                k, _, v = item.partition("=")
                arguments[k.strip()] = v.strip()
    result = bridge.call_tool(tool_name, arguments)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ============================================================
# 辅助函数
# ============================================================

def _format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"
