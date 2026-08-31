# dot.workflow.graph — WorkflowGraph（线性 + 条件分支）
#
# 图本身只负责编排。节点可以通过 WorkflowContext.interrupt() 暂停，
# 调用方收到 WorkflowInterruptEvent 后调用 ctx.resume(value) 继续执行。
from __future__ import annotations

import asyncio
import inspect
import logging
import random
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from .context import WorkflowContext
from .events import (
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowNodeEndEvent,
    WorkflowNodeStartEvent,
)
from .node import WorkflowNode

logger = logging.getLogger(__name__)

END = "__end__"

Router = Callable[["WorkflowContext"], str | None]


# ============================================================
# 策略与错误类型
# ============================================================


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """节点重试策略"""
    retries: int = 0
    timeout: float | None = None
    backoff_base: float = 1.0  # 指数退避基数（秒）
    backoff_max: float = 60.0  # 最大退避时间
    backoff_jitter: float = 0.1  # 抖动因子


@dataclass(frozen=True, slots=True)
class NodePolicy:
    """兼容性别名，保留旧接口"""
    retries: int = 0
    timeout: float | None = None

    @classmethod
    def from_retry_policy(cls, policy: RetryPolicy) -> "NodePolicy":
        return cls(retries=policy.retries, timeout=policy.timeout)


class WorkflowCancellationError(Exception):
    """内部异常：节点驱动器检测到 workflow 取消。"""


class WorkflowError(Exception):
    """结构化工作流错误"""
    def __init__(
        self,
        message: str,
        *,
        code: str = "WORKFLOW_ERROR",
        node: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.node = node
        self.details = details or {}


class WorkflowValidationError(WorkflowError):
    """图校验失败"""
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, code="VALIDATION_ERROR", **kwargs)


# ============================================================
# 补偿节点协议
# ============================================================


class CompensationNode(Protocol):
    """补偿节点协议：节点执行失败后自动执行的清理动作"""
    name: str

    async def compensate(self, ctx: "WorkflowContext", error: Exception) -> None:
        """执行补偿逻辑，失败不应抛出异常"""
        ...


@dataclass(frozen=True, slots=True)
class FunctionCompensationNode:
    """函数形式的补偿节点"""
    name: str
    fn: Callable[["WorkflowContext", Exception], Any]

    async def compensate(self, ctx: "WorkflowContext", error: Exception) -> None:
        try:
            result = self.fn(ctx, error)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.warning("[workflow] compensation %s failed: %s", self.name, e)


# ============================================================
# WorkflowGraph
# ============================================================


class WorkflowGraph:
    """workflow 定义 + 执行循环"""

    def __init__(
        self,
        *,
        name: str = "workflow",
        max_steps: int = 100,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.name = name
        self.max_steps = max_steps
        self._nodes: dict[str, WorkflowNode] = {}
        self._policies: dict[str, RetryPolicy] = {}
        self._compensations: dict[str, CompensationNode] = {}
        self._entry: str | None = None
        self._edges: dict[str, str] = {}
        self._routers: dict[str, Router] = {}

    # ============================================================
    # 定义期 API
    # ============================================================

    def add_node(
        self,
        node: WorkflowNode,
        *,
        retries: int = 0,
        timeout: float | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        backoff_jitter: float = 0.1,
        compensate_with: CompensationNode | None = None,
    ) -> "WorkflowGraph":
        """添加节点及其策略。
        
        Args:
            node: 节点实例
            retries: 最大重试次数
            timeout: 超时秒数（None 表示不限）
            backoff_base: 指数退避基数
            backoff_max: 最大退避时间
            backoff_jitter: 抖动因子 [0, 1)
            compensate_with: 失败时执行的补偿节点
        """
        if node.name in self._nodes:
            raise ValueError(f"duplicate node: {node.name}")
        if retries < 0:
            raise ValueError("retries must be non-negative")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        if backoff_jitter < 0 or backoff_jitter >= 1:
            raise ValueError("backoff_jitter must be in [0, 1)")

        self._nodes[node.name] = node
        self._policies[node.name] = RetryPolicy(
            retries=retries,
            timeout=timeout,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            backoff_jitter=backoff_jitter,
        )
        if compensate_with is not None:
            self._compensations[node.name] = compensate_with
        return self

    def add_compensation(
        self,
        node_name: str,
        compensation: CompensationNode,
    ) -> "WorkflowGraph":
        """为已有节点添加补偿节点"""
        self._require_node(node_name)
        self._compensations[node_name] = compensation
        return self

    def set_entry(self, name: str) -> "WorkflowGraph":
        self._require_node(name)
        self._entry = name
        return self

    def add_edge(self, source: str, target: str) -> "WorkflowGraph":
        self._require_node(source)
        if target != END:
            self._require_node(target)
        self._require_no_outgoing(source)
        self._edges[source] = target
        return self

    def add_conditional_edges(self, source: str, router: Router) -> "WorkflowGraph":
        self._require_node(source)
        self._require_no_outgoing(source)
        self._routers[source] = router
        return self

    def _require_node(self, name: str) -> None:
        if name not in self._nodes:
            raise ValueError(f"unknown node: {name}")

    def _require_no_outgoing(self, name: str) -> None:
        if name in self._edges or name in self._routers:
            raise ValueError(f"node {name} already has an outgoing edge")

    def _next_of(self, name: str, ctx: WorkflowContext) -> str | None:
        if name in self._routers:
            target = self._routers[name](ctx)
            if target not in (None, END):
                self._require_node(target)
            return None if target in (None, END) else target
        if name in self._edges:
            target = self._edges[name]
            return None if target == END else target
        return None

    def _get_successors(self, name: str) -> list[str]:
        """获取节点的所有后继节点（用于图分析）"""
        if name in self._edges:
            return [self._edges[name]] if self._edges[name] != END else []
        if name in self._routers:
            return []  # 条件边不确定后继
        return []

    # ============================================================
    # 图校验
    # ============================================================

    def validate(self) -> None:
        """完整图校验：入口、无效边、不可达节点、环路"""
        errors: list[str] = []

        # 1. 入口检查
        if self._entry is None:
            raise WorkflowValidationError("entry node is not set")

        # 2. 检查孤立节点（无入边且非入口）
        #    条件路由的目标在运行时才确定，无法静态分析，
        #    因此有 router 时跳过不可达检查。
        if not self._routers:
            all_targets: set[str] = set()
            for target in self._edges.values():
                if target != END:
                    all_targets.add(target)

            unreachable = [
                name for name in self._nodes
                if name != self._entry and name not in all_targets
            ]
            if unreachable:
                errors.append(f"unreachable nodes: {unreachable}")

        # 3. 出口检查：所有节点都必须可达 END
        #    条件分支的节点需要在 router 中显式处理 END
        exit_nodes = [
            name for name in self._nodes
            if name not in self._edges and name not in self._routers
        ]
        # 排除入口（可能一步就到 END）
        exit_without_path = [
            n for n in exit_nodes
            if n != self._entry and not self._has_path_to_end(n)
        ]
        if exit_without_path:
            errors.append(f"nodes without path to END: {exit_without_path}")

        # 4. 环路检测（DFS）
        cycles = self._detect_cycles()
        if cycles:
            errors.append(f"cycles detected: {cycles}")

        if errors:
            raise WorkflowValidationError("; ".join(errors))

    def _has_path_to_end(self, start: str, visited: set[str] | None = None) -> bool:
        """检查从 start 是否有路径到达 END"""
        if visited is None:
            visited = set()
        if start in visited:
            return False
        visited.add(start)

        if start in self._edges:
            return self._edges[start] == END or self._has_path_to_end(
                self._edges[start], visited
            )
        if start in self._routers:
            return True
        # 无出边的终端节点隐式结束工作流
        return True

    def _detect_cycles(self) -> list[list[str]]:
        """DFS 检测所有环路"""
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self._nodes}
        parent: dict[str, str | None] = {n: None for n in self._nodes}
        cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            color[node] = GREY
            for succ in self._get_successors(node):
                if color[succ] == GREY:
                    # 找到环路
                    cycle = [succ]
                    cur = node
                    while cur != succ:
                        cycle.append(cur)
                        cur = parent[cur]  # type: ignore
                    cycle.reverse()
                    cycles.append(cycle)
                elif color[succ] == WHITE:
                    parent[succ] = node
                    dfs(succ)
            color[node] = BLACK

        for node in self._nodes:
            if color[node] == WHITE:
                dfs(node)

        return cycles

    # ============================================================
    # 图导出
    # ============================================================

    def to_dict(self) -> dict[str, Any]:
        """导出为 dict（可序列化）"""
        return {
            "name": self.name,
            "max_steps": self.max_steps,
            "entry": self._entry,
            "nodes": {
                name: {
                    "policy": {
                        "retries": p.retries,
                        "timeout": p.timeout,
                        "backoff_base": p.backoff_base,
                        "backoff_max": p.backoff_max,
                        "backoff_jitter": p.backoff_jitter,
                    },
                    "has_compensation": name in self._compensations,
                }
                for name, p in self._policies.items()
            },
            "edges": dict(self._edges),
            "routers": list(self._routers.keys()),
        }

    def to_mermaid(self) -> str:
        """导出为 Mermaid flowchart 语法"""
        lines = [f"flowchart LR", f"    %% {self.name}"]

        # 节点定义
        for name in self._nodes:
            policy = self._policies.get(name)
            label = name
            if policy and (policy.retries > 0 or policy.timeout):
                attrs = []
                if policy.retries > 0:
                    attrs.append(f"r={policy.retries}")
                if policy.timeout:
                    attrs.append(f"t={policy.timeout}s")
                label = f"{name}\\n[{', '.join(attrs)}]"
            lines.append(f"    {name}(({label}))")

        # 边定义
        for source, target in self._edges.items():
            if target == END:
                lines.append(f"    {source} --> END((END))")
            else:
                lines.append(f"    {source} --> {target}")

        # 条件边
        for source in self._routers:
            lines.append(f"    {source} -.-> {source}_cond{{?}}")

        # 入口标记
        if self._entry:
            lines.append(f"    start((START)) --> {self._entry}")

        return "\n".join(lines)

    # ============================================================
    # 节点驱动
    # ============================================================

    @staticmethod
    async def _wait_for_cancel(ctx: WorkflowContext) -> None:
        while not ctx.signal.is_cancelled():
            await asyncio.sleep(0.05)

    async def _drive_node(
        self,
        node: WorkflowNode,
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

    def _compute_backoff(self, policy: RetryPolicy, attempt: int) -> float:
        """计算指数退避时间"""
        delay = policy.backoff_base * (2 ** (attempt - 1))
        delay = min(delay, policy.backoff_max)
        if policy.backoff_jitter > 0:
            jitter = delay * policy.backoff_jitter
            delay += random.uniform(-jitter, jitter)
        return max(0.0, delay)

    async def _run_with_compensation(
        self,
        node_name: str,
        ctx: WorkflowContext,
    ) -> None:
        """节点失败时执行补偿"""
        compensation = self._compensations.get(node_name)
        if compensation is None:
            return
        try:
            error = ctx.error
            exc = Exception(error) if error else Exception("unknown")
            await compensation.compensate(ctx, exc)
        except Exception as e:
            logger.warning("[workflow] compensation %s failed: %s", node_name, e)

    # ============================================================
    # 执行
    # ============================================================

    async def run(self, ctx: WorkflowContext | None = None) -> AsyncIterator[Any]:
        """从 entry 执行到 END，并透传节点事件。"""
        self.validate()
        if ctx is None:
            ctx = WorkflowContext()
        ctx._start_run(str(uuid4()))
        run_id = ctx.run_id or ""
        current: str | None = self._entry
        steps = 0
        start_time = asyncio.get_running_loop().time()
        ctx.start_time = start_time

        logger.info("[workflow:%s] start at %s", self.name, self._entry)
        try:
            while current is not None:
                ctx.current_node = current
                ctx.current_step = steps
                if ctx.signal.is_cancelled():
                    error = f"workflow cancelled at node {current}"
                    ctx.mark_cancelled(error)
                    yield WorkflowErrorEvent(
                        node=current,
                        error=error,
                        run_id=run_id,
                        step=steps,
                    )
                    return

                steps += 1
                if steps > self.max_steps:
                    error = f"max_steps={self.max_steps} exceeded at node {current}"
                    ctx.mark_error(error)
                    yield WorkflowErrorEvent(
                        node=current,
                        error=error,
                        run_id=run_id,
                        step=steps,
                    )
                    return

                node = self._nodes[current]
                policy = self._policies[current]
                yield WorkflowNodeStartEvent(
                    node=current,
                    run_id=run_id,
                    step=steps,
                )

                error: str | None = None
                attempts = 0
                last_exception: Exception | None = None

                while attempts <= policy.retries:
                    attempts += 1
                    try:
                        async for item in self._drive_node(node, ctx, policy.timeout):
                            yield item
                        error = None
                        break
                    except WorkflowCancellationError:
                        error = f"workflow cancelled at node {current}"
                        break
                    except TimeoutError as exc:
                        last_exception = exc
                        error = f"TimeoutError: {exc}"
                        if attempts <= policy.retries:
                            delay = self._compute_backoff(policy, attempts)
                            logger.warning(
                                "[workflow:%s] retry node %s (%d/%d) after %.2fs: %s",
                                self.name, current, attempts, policy.retries, delay, error,
                            )
                            if delay > 0:
                                await asyncio.sleep(delay)
                            continue
                    except Exception as exc:  # noqa: BLE001
                        last_exception = exc
                        error = f"{type(exc).__name__}: {exc}"
                        if attempts <= policy.retries:
                            delay = self._compute_backoff(policy, attempts)
                            logger.warning(
                                "[workflow:%s] retry node %s (%d/%d) after %.2fs: %s",
                                self.name, current, attempts, policy.retries, delay, error,
                            )
                            if delay > 0:
                                await asyncio.sleep(delay)
                            continue

                if error is not None:
                    if error.startswith("workflow cancelled"):
                        ctx.mark_cancelled(error)
                    else:
                        ctx.mark_error(error)
                    # 执行补偿节点
                    await self._run_with_compensation(current, ctx)
                    yield WorkflowNodeEndEvent(
                        node=current,
                        ok=False,
                        error=error,
                        run_id=run_id,
                        attempts=attempts,
                        step=steps,
                        duration=asyncio.get_running_loop().time() - start_time,
                    )
                    yield WorkflowErrorEvent(
                        node=current,
                        error=error,
                        run_id=run_id,
                        step=steps,
                        details={"attempts": attempts, "last_exception": str(last_exception)} if last_exception else None,
                    )
                    return

                ctx.completed_nodes.append(current)
                yield WorkflowNodeEndEvent(
                    node=current,
                    run_id=run_id,
                    attempts=attempts,
                    step=steps,
                    duration=asyncio.get_running_loop().time() - start_time,
                )
                if ctx.signal.is_cancelled():
                    error = f"workflow cancelled at node {current}"
                    ctx.mark_cancelled(error)
                    yield WorkflowErrorEvent(
                        node=current,
                        error=error,
                        run_id=run_id,
                        step=steps,
                    )
                    return
                try:
                    current = self._next_of(current, ctx)
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    ctx.mark_error(error)
                    yield WorkflowErrorEvent(
                        node=ctx.current_node or "",
                        error=error,
                        run_id=run_id,
                        step=steps,
                    )
                    return

            ctx.status = "completed"
            ctx.current_node = None
            ctx.current_step = steps
            duration = asyncio.get_running_loop().time() - start_time
            logger.info("[workflow:%s] done in %.2fs", self.name, duration)
            yield WorkflowDoneEvent(
                run_id=run_id,
                step=steps,
                duration=duration,
            )
        finally:
            ctx._detach_run()
