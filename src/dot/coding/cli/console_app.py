"""
dot.coding.cli.console — Simple console mode

Chat-style REPL: user input and assistant output look like a conversation.
Internal events logged via logging for debugging.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from dot import AgentHarness
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode

log = logging.getLogger("console")


def run_console(
        workspace: Path,
        mode: str = "auto",
        verbose: bool = False,
        session_id: str | None = None,
) -> int:
    """Run the console REPL — persistent event loop, no generator cleanup errors"""
    # 统一日志：stderr + .dot/logs/dot.log
    from dot.coding.logging_config import setup as setup_logging
    setup_logging(workspace=workspace, level="DEBUG" if verbose else "INFO")

    agent_mode = AgentMode.from_str(mode)

    # 构建 host 对象
    host = CodingHost(
        workspace=workspace,
        mode=agent_mode,
        extra_extension_dirs=[
            Path(__file__).resolve().parents[2] / ".dot" / "extensions"
        ],
    )

    # 恢复会话
    if session_id:
        if not host.resume_session(session_id):
            log.info(f"session not found: {session_id}, starting new session")
        else:
            log.info(f"resumed {session_id} ({len(host.session.messages)} messages)")

    # 创建一个 harness 对象 ==》 agent
    harness = host.create_harness(system="You are a helpful coding assistant.")

    # > 这段是 Python `asyncio` 异步代码：**新建一个独立事件循环，在这个循环里跑异步`_console_loop`，捕获 Ctrl+C 中断，关闭时屏蔽 stderr 报错再做清理**。

    # `loop = asyncio.new_event_loop()`
    # Python asyncio 事件循环，本质是**一个线程 + 任务队列**，跑所有`async def`协程。
    # > Java 类比：Netty `NioEventLoop`，一个线程不断轮询 IO 事件、调度任务。
    # > Python 协程不是操作系统线程，是用户态的，全部跑在这个 loop 绑定的线程内。
    loop = asyncio.new_event_loop()
    # 给当前线程绑定这个新建的 loop。Python 线程和 event_loop 是 1 对 1 的；不 set 的话`asyncio.get_event_loop()`拿不到你新建的这个。
    # 后面可以：
    # asyncio.get_event_loop()   # 获取当前线程绑定的loop
    # asyncio.get_running_loop() # 获取当前正在跑的loop
    asyncio.set_event_loop(loop)
    try:
        # - `_console_loop(...)`只是得到一个协程对象，**不会执行**；
        # - `run_until_complete`：**当前调用线程阻塞住，启动事件循环，驱动协程运行，直到协程 return / 抛出异常，方法才返回**。

        # // Python: loop.run_until_complete(asyncFunc())
        # // Java CompletableFuture风格，阻塞等待异步任务完成
        # CompletableFuture<Void> future = asyncFunc();
        # future.join(); // 当前线程阻塞，等待异步任务结束
        loop.run_until_complete(_console_loop(harness, host))
    except KeyboardInterrupt as e:
        log.error(f"Ctrl+C pressed, exiting" + str(e))
    finally:
        # Suppress stderr during cleanup to hide PyCharm debugger tracing errors
        # from openai SDK async generator (GeneratorExit / AsyncLibraryNotFoundError).
        # These are expected during shutdown and don't affect functionality.
        # OpenAI SDK 异步生成器在程序关闭时，会抛一些内部调试异常（`GeneratorExit`、`AsyncLibraryNotFoundError`），
        # PyCharm 调试器会把这些堆栈打印到 stderr，属于**无害的预期报错，不影响功能，但很干扰控制台**。
        # 于是：**清理阶段临时把 stderr 输出丢到黑洞，不让这些无关报错打印出来**，清理完恢复 stderr。
        _devnull = open(os.devnull, "w")
        # 做事件循环关闭：取消剩余协程任务、关闭 IO 资源、关闭 loop，释放 harness 业务资源。
        with contextlib.redirect_stderr(_devnull):
            _cleanup_loop(loop, harness)
        _devnull.close()
    return 0


def _cleanup_loop(loop, harness) -> None:
    """Cancel tasks and shut down async generators before closing the loop"""
    harness.cancel()
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    except Exception:
        pass
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except RuntimeError:
        pass
    except Exception:
        pass
    loop.close()


def _log_available_commands(commands, host) -> None:
    """打印所有可用命令到日志"""
    builtins = commands.list_commands()
    ext_cmds = host.extensions.commands
    parts = [f"/{c.name}" for c in builtins]
    parts.extend(f"/{name}" for name in ext_cmds)
    log.info("可用命令: [%s]", ", ".join(parts))


async def _console_loop(harness, host) -> None:
    """Async REPL loop — runs entirely inside one event loop"""
    from dot.coding.commands import get_command_registry

    commands = get_command_registry()
    commands.set_host(host)

    while True:
        try:
            # 打印可用命令
            _log_available_commands(commands, host)
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("bye")
            return

        if not user_input:
            continue

        if user_input.startswith("/"):
            result = commands.execute(user_input)
            if result.kind == "quit":
                print("bye")
                return
            if result.kind == "toast":
                print(f"[{result.level}] {result.text}")
            elif result.kind == "message":
                print(result.text)
            continue

        print(f"you> {user_input}")
        try:
            await _run_turn(harness, user_input)
            host.save_session()
        except asyncio.CancelledError:
            print("interrupted")
        except Exception as exc:
            log.error("Turn failed: %s", exc, exc_info=True)
            print(f"error: {exc}")


async def _run_turn(harness: AgentHarness, user_input: str) -> None:
    """Run one agent turn, buffer text deltas, print assistant message when done"""
    log.info("User: %s", user_input[:200])

    buffer: list[str] = []
    thinking_buffer: list[str] = []

    try:
        async for event in harness.prompt(user_input):
            _process_event(event, buffer, thinking_buffer)
    except asyncio.CancelledError:
        log.info("Turn cancelled")
        # Flush partial text
        if buffer:
            print("".join(buffer))
        raise


def _process_event(event, buffer: list[str], thinking_buffer: list[str]) -> None:
    """Process event: accumulate text deltas, flush on message end"""
    from dot.agent.events import (
        AgentEndEvent, AgentStartEvent, MessageEndEvent, MessageStartEvent,
        MessageUpdateEvent, ToolExecutionEndEvent, ToolExecutionStartEvent,
        ToolExecutionUpdateEvent, TurnEndEvent, TurnStartEvent,
    )
    from dot.ai.events import TextDeltaEvent, ThinkingDeltaEvent
    from dot.ai.types import AssistantMessage

    if isinstance(event, AgentStartEvent):
        log.debug("[agent] start")
    elif isinstance(event, AgentEndEvent):
        log.debug("[agent] end")
    elif isinstance(event, TurnStartEvent):
        pass
    elif isinstance(event, TurnEndEvent):
        pass
    elif isinstance(event, MessageStartEvent):
        msg = event.message
        if isinstance(msg, AssistantMessage) and msg.text:
            buffer.append(msg.text)
    elif isinstance(event, MessageEndEvent):
        msg = event.message
        if isinstance(msg, AssistantMessage):
            text = "".join(buffer)
            if text:
                print(text)
            buffer.clear()
            log.info("[assistant] %s", text[:500])
    elif isinstance(event, MessageUpdateEvent):
        nested = getattr(event, "provider_event", None)
        if isinstance(nested, TextDeltaEvent):
            buffer.append(nested.delta)
        elif isinstance(nested, ThinkingDeltaEvent):
            thinking_buffer.append(nested.delta)
    elif isinstance(event, ToolExecutionStartEvent):
        print(f"- {event.tool_name}")
    elif isinstance(event, ToolExecutionUpdateEvent):
        pass  # skip intermediate updates
    elif isinstance(event, ToolExecutionEndEvent):
        status = "OK" if not event.is_error else "FAIL"
        result_text = event.result.text[:200] if event.result else ""
        print(f"  {status}: {result_text}")


def _execute_slash(text: str, host):
    """Execute slash command"""
    from dot.coding.commands import get_command_registry
    return get_command_registry().execute(text)


def approval_handler(info: dict) -> bool:
    """Simple approval handler"""
    log.info(f"permission required: {info.get('tool_name', '')} ({info.get('reason', '')})")
    try:
        answer = input("  approve? Y/N: ").strip().lower()
        result = answer in ("y", "yes")
        log.info("approval: %s", result)
        return result
    except (EOFError, KeyboardInterrupt):
        return False
