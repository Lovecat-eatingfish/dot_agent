# dot.workflow.events — WorkflowEvent discriminated union
#
# 与 AgentEvent / CodingEvent 同一范式：
# WireModel + type: Literal 判别字段 + PEP 604 discriminated union。
#
# 引擎产物流为 WorkflowEvent | AgentEvent：
# 引擎只发自己的生命周期事件，节点 yield 出来的内部事件
# （如 AgentEvent）原样透传，不做包装。
from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import Field

from dot.core.wire import WireModel


class WorkflowNodeStartEvent(WireModel):
    """即将执行某个节点"""
    type: Literal["workflow_node_start"] = "workflow_node_start"
    node: str
    run_id: str = ""
    step: int = 1
    timestamp: float = Field(default_factory=time.time)


class WorkflowNodeEndEvent(WireModel):
    """某个节点执行完毕"""
    type: Literal["workflow_node_end"] = "workflow_node_end"
    node: str
    ok: bool = True
    error: str = ""
    run_id: str = ""
    attempts: int = 1
    step: int = 1
    duration: float = 0.0
    timestamp: float = Field(default_factory=time.time)


class WorkflowErrorEvent(WireModel):
    """workflow 因异常/超限终止"""
    type: Literal["workflow_error"] = "workflow_error"
    node: str = ""
    error: str
    run_id: str = ""
    step: int = 0
    details: dict[str, Any] | None = None
    timestamp: float = Field(default_factory=time.time)


class WorkflowDoneEvent(WireModel):
    """workflow 正常走完"""
    type: Literal["workflow_done"] = "workflow_done"
    run_id: str = ""
    step: int = 0
    duration: float = 0.0
    timestamp: float = Field(default_factory=time.time)


class WorkflowInterruptEvent(WireModel):
    """workflow 等待外部决定时发出的可恢复中断"""
    type: Literal["workflow_interrupt"] = "workflow_interrupt"
    interrupt_id: str
    node: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""
    step: int = 0
    timestamp: float = Field(default_factory=time.time)


# ============================================================
# 结构化错误分类
# ============================================================


class ErrorCode:
    """错误码常量（可扩展）"""
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TIMEOUT = "TIMEOUT"
    NODE_ERROR = "NODE_ERROR"
    ROUTING_ERROR = "ROUTING_ERROR"
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    CANCELLED = "CANCELLED"
    INTERRUPT_TIMEOUT = "INTERRUPT_TIMEOUT"
    UNKNOWN = "UNKNOWN"


type WorkflowEvent = Annotated[
    WorkflowNodeStartEvent
    | WorkflowNodeEndEvent
    | WorkflowErrorEvent
    | WorkflowDoneEvent
    | WorkflowInterruptEvent,
    Field(discriminator="type"),
]
