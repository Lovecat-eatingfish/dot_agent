# dot.workflow.graph — WorkflowGraph（图定义）
#
# 图本身只负责编排定义（节点 / 边 / 策略 / 补偿），静态校验委托 GraphValidator，
# 序列化导出委托 graph_export，执行循环委托 GraphRunner。
# 节点可以通过 WorkflowContext.interrupt() 暂停，
# 调用方收到 WorkflowInterruptEvent 后调用 ctx.resume(value) 继续执行。
from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .context import WorkflowContext
from .graph_export import graph_to_dict, graph_to_mermaid
from .graph_runner import GraphRunner
from .graph_types import (
    END,
    NodePolicy,
    RetryPolicy,
    Router,
    WorkflowCancellationError,
    WorkflowError,
    WorkflowValidationError,
)
from .graph_validate import GraphValidator
from .node import WorkflowNode

logger = logging.getLogger(__name__)

__all__ = [
    "END",
    "CompensationNode",
    "FunctionCompensationNode",
    "NodePolicy",
    "RetryPolicy",
    "Router",
    "WorkflowCancellationError",
    "WorkflowError",
    "WorkflowGraph",
    "WorkflowValidationError",
]


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
    """workflow 图定义（节点 / 边 / 策略），执行委托 GraphRunner"""

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
        self._runner = GraphRunner(self)

    @property
    def entry(self) -> str | None:
        """入口节点名"""
        return self._entry

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

    # ============================================================
    # 图校验（委托 GraphValidator）
    # ============================================================

    def validate(self) -> None:
        """完整图校验：入口、无效边、不可达节点、环路"""
        GraphValidator(
            nodes=self._nodes,
            entry=self._entry,
            edges=self._edges,
            routers=self._routers,
        ).validate()

    # ============================================================
    # 图导出（委托 graph_export）
    # ============================================================

    def to_dict(self) -> dict[str, Any]:
        """导出为 dict（可序列化）"""
        return graph_to_dict(self)

    def to_mermaid(self) -> str:
        """导出为 Mermaid flowchart 语法"""
        return graph_to_mermaid(self)

    # ============================================================
    # 执行（委托 GraphRunner）
    # ============================================================

    def run(self, ctx: WorkflowContext | None = None) -> AsyncIterator[Any]:
        """从 entry 执行到 END，并透传节点事件。"""
        return self._runner.run(ctx)
