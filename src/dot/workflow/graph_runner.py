# dot.workflow.graph_runner — GraphRunner（图执行器）
#
# 只负责执行：节点驱动、重试/退避、补偿、取消与步数上限。
# 与图定义解耦：WorkflowGraph.run() 委托到这里。
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .context import WorkflowContext
from .events import (
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowNodeEndEvent,
    WorkflowNodeStartEvent,
)
from .graph_types import RetryPolicy, WorkflowCancellationError

if TYPE_CHECKING:
    from .graph import WorkflowGraph
    from .node import WorkflowNode

logger = logging.getLogger(__name__)

END = "__end__"


@dataclass
class NodeExecutionResult:
    """单节点执行的最终结果（供生成器向调用方回传）"""
    error: str | None = None
    attempts: int = 0
    last_exception: Exception | None = None


class GraphRunner:
    """驱动一张已定义的图从 entry 执行到 END"""

    def __init__(self, graph: "WorkflowGraph") -> None:
        self._graph = graph

    # ============================================================
    # 对外入口
    # ============================================================

    async def run(self, ctx: WorkflowContext | None = None) -> AsyncIterator[Any]:
        """从 entry 执行到 END，并透传节点事件。"""
        self._graph.validate()
        if ctx is None:
            ctx = WorkflowContext()
        ctx._start_run(str(uuid4()))
        ctx.workflow_name = self._graph.name
        run_id = ctx.run_id or ""
        current: str | None = self._graph.entry
        steps = 0
        start_time = asyncio.get_running_loop().time()
        ctx.start_time = start_time

        logger.info("[workflow:%s] start at %s", self._graph.name, self._graph.entry)
        try:
            while current is not None:
                ctx.current_node = current
                ctx.current_step = steps

                # 取消守卫
                cancel_error = self._cancel_error(current, ctx)
                if cancel_error is not None:
                    yield self._error_event(current, cancel_error, run_id, steps)
                    return

                # 步数上限守卫
                steps += 1
                max_steps_error = self._max_steps_error(current, steps, ctx)
                if max_steps_error is not None:
                    yield self._error_event(current, max_steps_error, run_id, steps)
                    return

                node = self._graph._nodes[current]
                policy = self._graph._policies[current]
                yield WorkflowNodeStartEvent(
                    node=current,
                    run_id=run_id,
                    step=steps,
                )

                result = NodeExecutionResult()
                async for item in self._execute_node(node, policy, current, ctx, result):
                    yield item

                if result.error is not None:
                    async for event in self._fail_node(
                        current, ctx, run_id, steps, result,
                    ):
                        yield event
                    return

                ctx.completed_nodes.append(current)
                yield self._node_end_event(current, run_id, result.attempts, steps, start_time, ok=True)
                if ctx.signal.is_cancelled():
                    after_error = self._cancel_error(current, ctx)
                    yield self._error_event(current, after_error or "", run_id, steps)
                    return

                next_node, advance_error = self._advance(current, ctx)
                if advance_error is not None:
                    ctx.mark_error(advance_error)
                    yield self._error_event(ctx.current_node or "", advance_error, run_id, steps)
                    return
                current = next_node

            ctx.status = "completed"
            ctx.current_node = None
            ctx.current_step = steps
            duration = asyncio.get_running_loop().time() - start_time
            logger.info("[workflow:%s] done in %.2fs", self._graph.name, duration)
            yield WorkflowDoneEvent(
                run_id=run_id,
                step=steps,
                duration=duration,
            )
        finally:
            ctx._detach_run()

    # ============================================================
    # 单一职责：守卫 / 执行 / 失败 / 推进
    # ============================================================

    @staticmethod
    def _cancel_error(current: str, ctx: WorkflowContext) -> str | None:
        """取消守卫：已取消时标记 ctx 并返回错误文本"""
        if ctx.signal.is_cancelled():
            error = f"workflow cancelled at node {current}"
            ctx.mark_cancelled(error)
            return error
        return None

    def _max_steps_error(self, current: str, steps: int, ctx: WorkflowContext) -> str | None:
        """步数上限守卫：超限时标记 ctx 并返回错误文本"""
        if steps > self._graph.max_steps:
            error = f"max_steps={self._graph.max_steps} exceeded at node {current}"
            ctx.mark_error(error)
            return error
        return None

    async def _execute_node(
        self,
        node: "WorkflowNode",
        policy: RetryPolicy,
        current: str,
        ctx: WorkflowContext,
        result: NodeExecutionResult,
    ) -> AsyncIterator[Any]:
        """执行单个节点：透传节点产出，含重试/退避；最终结果写入 result"""
        attempts = 0
        while attempts <= policy.retries:
            attempts += 1
            result.attempts = attempts
            try:
                async for item in self._drive_node(node, ctx, policy.timeout):
                    yield item
                result.error = None
                break
            except WorkflowCancellationError:
                result.error = f"workflow cancelled at node {current}"
                break
            except Exception as exc:  # noqa: BLE001
                result.last_exception = exc
                result.error = f"{type(exc).__name__}: {exc}"
                if attempts <= policy.retries:
                    await self._retry_backoff(policy, current, attempts, result.error)
                    continue

    async def _fail_node(
        self,
        current: str,
        ctx: WorkflowContext,
        run_id: str,
        steps: int,
        result: NodeExecutionResult,
    ) -> AsyncIterator[Any]:
        """节点失败收尾：标记状态、执行补偿、产出 NodeEnd(ok=False) 与 ErrorEvent"""
        error = result.error or "unknown"
        if error.startswith("workflow cancelled"):
            ctx.mark_cancelled(error)
        else:
            ctx.mark_error(error)
        await self._run_with_compensation(current, ctx)
        start_time = ctx.start_time
        yield WorkflowNodeEndEvent(
            node=current,
            ok=False,
            error=error,
            run_id=run_id,
            attempts=result.attempts,
            step=steps,
            duration=asyncio.get_running_loop().time() - start_time,
        )
        yield WorkflowErrorEvent(
            node=current,
            error=error,
            run_id=run_id,
            step=steps,
            details={"attempts": result.attempts, "last_exception": str(result.last_exception)}
            if result.last_exception else None,
        )

    def _advance(self, current: str, ctx: WorkflowContext) -> tuple[str | None, str | None]:
        """路由到下一节点，返回 (next_node, error)"""
        try:
            return self._next_of(current, ctx), None
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    # ============================================================
    # 内部机制
    # ============================================================

    def _error_event(self, node: str, error: str, run_id: str, steps: int) -> WorkflowErrorEvent:
        return WorkflowErrorEvent(node=node, error=error, run_id=run_id, step=steps)

    def _node_end_event(
        self, node: str, run_id: str, attempts: int, steps: int,
        start_time: float, *, ok: bool,
    ) -> WorkflowNodeEndEvent:
        return WorkflowNodeEndEvent(
            node=node,
            run_id=run_id,
            attempts=attempts,
            step=steps,
            duration=asyncio.get_running_loop().time() - start_time,
        )

    async def _retry_backoff(self, policy: RetryPolicy, current: str, attempts: int, error: str) -> None:
        """计算退避并等待（重试路径）"""
        delay = self._compute_backoff(policy, attempts)
        logger.warning(
            "[workflow:%s] retry node %s (%d/%d) after %.2fs: %s",
            self._graph.name, current, attempts, policy.retries, delay, error,
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _next_of(self, name: str, ctx: WorkflowContext) -> str | None:
        """解析节点的下一个目标（条件路由优先于固定边）"""
        if name in self._graph._routers:
            target = self._graph._routers[name](ctx)
            if target not in (None, END):
                self._graph._require_node(target)
            return None if target in (None, END) else target
        if name in self._graph._edges:
            target = self._graph._edges[name]
            return None if target == END else target
        return None

    def _compute_backoff(self, policy: RetryPolicy, attempt: int) -> float:
        """计算指数退避时间"""
        delay = policy.backoff_base * (2 ** (attempt - 1))
        delay = min(delay, policy.backoff_max)
        if policy.backoff_jitter > 0:
            jitter = delay * policy.backoff_jitter
            delay += random.uniform(-jitter, jitter)
        return max(0.0, delay)

    async def _run_with_compensation(self, node_name: str, ctx: WorkflowContext) -> None:
        """节点失败时执行补偿"""
        compensation = self._graph._compensations.get(node_name)
        if compensation is None:
            return
        try:
            error = ctx.error
            exc = Exception(error) if error else Exception("unknown")
            await compensation.compensate(ctx, exc)
        except Exception as e:
            logger.warning("[workflow] compensation %s failed: %s", node_name, e)

    @staticmethod
    async def _wait_for_cancel(ctx: WorkflowContext) -> None:
        while not ctx.signal.is_cancelled():
            await asyncio.sleep(0.05)

    async def _drive_node(
        self,
        node: "WorkflowNode",
        ctx: WorkflowContext,
        timeout: float | None,
    ) -> AsyncIterator[Any]:
        """驱动节点，同时转发节点事件和可恢复中断。"""
        iterator = node.run(ctx).__aiter__()

        async def next_item() -> tuple[bool, Any]:
            try:
                return True, await iterator.__anext__()
            except StopAsyncIteration:
                return False, None

        next_task = asyncio.create_task(next_item())
        cancel_task = asyncio.create_task(self._wait_for_cancel(ctx))
        interrupt_task: asyncio.Task | None = None
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout

        try:
            while True:
                interrupt_task = asyncio.create_task(ctx._next_interrupt())
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait(
                    {next_task, interrupt_task, cancel_task},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    next_task.cancel()
                    interrupt_task.cancel()
                    await asyncio.gather(next_task, interrupt_task, return_exceptions=True)
                    raise TimeoutError(f"node {node.name} timed out after {timeout}s")

                if cancel_task in done:
                    next_task.cancel()
                    interrupt_task.cancel()
                    await asyncio.gather(next_task, interrupt_task, return_exceptions=True)
                    raise WorkflowCancellationError

                if interrupt_task in done:
                    yield interrupt_task.result()
                    if next_task not in done:
                        continue
                else:
                    interrupt_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await interrupt_task

                try:
                    has_item, item = next_task.result()
                except asyncio.CancelledError:
                    if ctx.signal.is_cancelled():
                        raise WorkflowCancellationError
                    raise
                if not has_item:
                    return
                yield item
                next_task = asyncio.create_task(next_item())
        finally:
            next_task.cancel()
            cancel_task.cancel()
            if interrupt_task is not None:
                interrupt_task.cancel()
            await asyncio.gather(
                next_task,
                cancel_task,
                *(task for task in (interrupt_task,) if task is not None),
                return_exceptions=True,
            )
            with suppress(asyncio.CancelledError, Exception):
                await iterator.aclose()
