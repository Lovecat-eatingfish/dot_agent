# dot.workflow.context — WorkflowContext 通用状态容器
#
# 节点间通信的唯一通道：
#   - data:     任意键值（业务状态对象挂在这里）
#   - results:  节点名 → 该节点的最终产物
#   - signal:   只读取消令牌，节点在长耗时步骤间检查
#
# 引擎不解释 data 的内容，业务语义完全由节点定义。
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from .cancel import SimpleWorkflowCancellationToken
from .events import WorkflowInterruptEvent

WorkflowStatus = Literal[
    "pending", "running", "paused", "completed", "failed", "cancelled",
]


@dataclass
class WorkflowContext:
    """一个 workflow 运行实例的全部状态"""
    data: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    signal: SimpleWorkflowCancellationToken = field(
        default_factory=SimpleWorkflowCancellationToken,
    )
    run_id: str | None = None
    workflow_name: str | None = None
    status: WorkflowStatus = "pending"
    current_node: str | None = None
    current_step: int = 0
    completed_nodes: list[str] = field(default_factory=list)
    start_time: float | None = None
    error_code: str | None = None
    error_details: dict[str, Any] | None = None
    _interrupt_queue: asyncio.Queue[WorkflowInterruptEvent] | None = field(
        default=None, init=False, repr=False,
    )
    _pending_interrupts: dict[str, asyncio.Future[Any]] = field(
        default_factory=dict, init=False, repr=False,
    )

    def _start_run(self, run_id: str | None = None) -> None:
        if self.status in {"running", "paused"}:
            raise RuntimeError("workflow context is already running")
        self.run_id = run_id or str(uuid4())
        self.status = "running"
        self.current_node = None
        self.current_step = 0
        self.completed_nodes.clear()
        self.error = None
        self.error_code = None
        self.error_details = None
        self._interrupt_queue = asyncio.Queue()
    def _detach_run(self) -> None:
        for future in self._pending_interrupts.values():
            if not future.done():
                future.cancel()
        self._interrupt_queue = None
        self._pending_interrupts.clear()
        if self.status in {"running", "paused"}:
            self.status = "cancelled"
            if self.error is None:
                self.error = "workflow stream closed before completion"

    async def interrupt(
        self,
        reason: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """暂停当前节点，等待调用方通过 resume() 提供结果。"""
        queue = self._interrupt_queue
        if queue is None:
            raise RuntimeError("workflow context is not running")
        if self.signal.is_cancelled():
            raise asyncio.CancelledError

        interrupt_id = str(uuid4())
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_interrupts[interrupt_id] = future
        self.status = "paused"
        await queue.put(WorkflowInterruptEvent(
            interrupt_id=interrupt_id,
            node=self.current_node or "",
            reason=reason,
            payload=payload or {},
            run_id=self.run_id or "",
            step=self.current_step,
        ))
        try:
            return await future
        finally:
            await self._pending_interrupts.pop(interrupt_id, None)
            if self.status == "paused" and not self._pending_interrupts:
                self.status = "running"

    def resume(self, value: Any = None, *, interrupt_id: str | None = None) -> bool:
        """恢复一个待处理的中断；返回是否成功找到目标中断。"""
        target_id = interrupt_id
        if target_id is None and len(self._pending_interrupts) == 1:
            target_id = next(iter(self._pending_interrupts))
        if target_id is None:
            return False
        future = self._pending_interrupts.get(target_id)
        if future is None or future.done():
            return False
        self.status = "running"
        future.set_result(value)
        return True

    async def _next_interrupt(self) -> WorkflowInterruptEvent:
        queue = self._interrupt_queue
        if queue is None:
            raise RuntimeError("workflow context is not running")
        return await queue.get()

    def set_result(self, node: str, value: Any) -> None:
        self.results[node] = value

    def get_result(self, node: str, default: Any = None) -> Any:
        return self.results.get(node, default)

    def mark_error(
        self,
        error: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.error is None:
            self.error = error
        if self.error_code is None:
            self.error_code = code
        if self.error_details is None:
            self.error_details = details
        self.status = "failed"

    def mark_cancelled(self, error: str) -> None:
        if self.error is None:
            self.error = error
        if self.error_code is None:
            self.error_code = "CANCELLED"
        self.status = "cancelled"

    def to_report(self) -> dict[str, Any]:
        """生成运行报告"""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "workflow_name": getattr(self, "workflow_name", None),
            "current_node": self.current_node,
            "completed_nodes": list(self.completed_nodes),
            "current_step": self.current_step,
            "start_time": self.start_time,
            "error": self.error,
            "error_code": self.error_code,
            "error_details": self.error_details,
            "results": dict(self.results),
        }
