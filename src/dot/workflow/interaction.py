# dot.workflow.interaction — UI-independent workflow interrupt handlers
#
# 通用层只暴露 run_with_interaction() 和 handler 协议。
# console/TUI 实现应放在 dot.coding.cli.console_app，不放在这里。
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from inspect import isawaitable
from typing import Any

from .context import WorkflowContext
from .events import WorkflowInterruptEvent
from .graph import WorkflowGraph

WorkflowInteractionHandler = Callable[
    [WorkflowInterruptEvent], Any | Awaitable[Any]
]


async def run_with_interaction(
    graph: WorkflowGraph,
    ctx: WorkflowContext,
    handler: WorkflowInteractionHandler,
) -> AsyncIterator[Any]:
    """运行图，并把每个 interrupt 交给外部交互处理器。

    中断事件先透传给调用方，同时 handler 在后台等待用户输入；
    这样 TUI 可以先刷新界面，之后再通过 `ctx.resume` 恢复节点。
    """
    async def resolve(event: WorkflowInterruptEvent) -> Any:
        value = handler(event)
        if isawaitable(value):
            return await value
        return value

    async for event in graph.run(ctx):
        if isinstance(event, WorkflowInterruptEvent):
            resolution = asyncio.create_task(resolve(event))
            try:
                yield event
                value = await resolution
            except asyncio.CancelledError:
                resolution.cancel()
                await asyncio.gather(resolution, return_exceptions=True)
                raise
            except Exception:
                resolution.cancel()
                await asyncio.gather(resolution, return_exceptions=True)
                raise
            if not ctx.resume(value, interrupt_id=event.interrupt_id):
                raise RuntimeError(
                    f"workflow interrupt is no longer pending: {event.interrupt_id}"
                )
        else:
            yield event


__all__ = [
    "WorkflowInteractionHandler",
    "run_with_interaction",
]
