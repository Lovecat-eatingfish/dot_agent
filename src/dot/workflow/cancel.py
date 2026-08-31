"""
dot.workflow.cancel — Workflow 取消令牌

与 dot.agent.cancel 同构：节点只拿到只读视图，
可写能力保留在调用方/引擎一侧，形成清晰的权限边界。
"""
from __future__ import annotations

from typing import Protocol


class WorkflowCancellationToken(Protocol):
    """Workflow 节点取消令牌（只读）"""

    def is_cancelled(self) -> bool:
        """当前 workflow 是否应停止"""
        ...


class SimpleWorkflowCancellationToken:
    """简单的可写取消令牌（调用方持有，通过 WorkflowContext 暴露只读视图）"""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled
