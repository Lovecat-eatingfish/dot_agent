# dot.workflow.graph_types — 工作流图的策略与错误类型
#
# 独立于图定义/执行，供 graph.py（定义）与 graph_validate.py（校验）共同引用，
# 避免"校验器依赖图、图又依赖校验器"的循环。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import WorkflowContext

END = "__end__"

Router = Callable[["WorkflowContext"], str | None]


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
