# dot.workflow — 通用工作流引擎（第四层）
#
# 节点不限于 coding agent：任何 WorkflowNode 协议的实现都能编排，
# FunctionNode 是核心提供的通用节点。Agent/LLM 节点位于上层适配器，
# coding 层的 plan→code→validate 是它的一个业务实例，不是引擎的一部分。
#
# 核心不依赖具体 Agent、模型 SDK 或编排框架。
from __future__ import annotations

from .cancel import SimpleWorkflowCancellationToken, WorkflowCancellationToken
from .context import WorkflowContext, WorkflowStatus
from .events import (
    ErrorCode,
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowEvent,
    WorkflowInterruptEvent,
    WorkflowNodeEndEvent,
    WorkflowNodeStartEvent,
)
from .graph import (
    END,
    CompensationNode,
    FunctionCompensationNode,
    NodePolicy,
    RetryPolicy,
    WorkflowCancellationError,
    WorkflowError,
    WorkflowGraph,
    WorkflowValidationError,
)
from .interaction import (
    WorkflowInteractionHandler,
    run_with_interaction,
)
from .node import FunctionNode, WorkflowNode


def __getattr__(name: str):
    """Keep the old `dot.workflow.AgentNode` import lazy and compatible."""
    if name == "AgentNode":
        from dot.agent.workflow import AgentNode

        return AgentNode
    raise AttributeError(name)


__all__ = [
    # graph
    "WorkflowGraph",
    "NodePolicy",
    "RetryPolicy",
    "WorkflowCancellationError",
    "WorkflowError",
    "WorkflowValidationError",
    "END",
    # compensation
    "CompensationNode",
    "FunctionCompensationNode",
    # context / cancel
    "WorkflowContext",
    "WorkflowStatus",
    "WorkflowCancellationToken",
    "SimpleWorkflowCancellationToken",
    # nodes
    "WorkflowNode",
    "FunctionNode",
    # compatibility export; implementation lives in dot.agent.workflow
    # events
    "WorkflowEvent",
    "WorkflowNodeStartEvent",
    "WorkflowNodeEndEvent",
    "WorkflowErrorEvent",
    "WorkflowInterruptEvent",
    "WorkflowDoneEvent",
    "ErrorCode",
    # interaction
    "WorkflowInteractionHandler",
    "run_with_interaction",
]
