"""
dot.coding.workflow — run_workflow（外层循环）

普通 Python 代码 + 显式状态机（WorkflowPhase），
替代 LangGraph 的 StateGraph + 条件边。
阶段转换用 if/else，比 LangGraph 条件边直观。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from dot.agent.events import AgentEvent

from .state import WorkflowContext, WorkflowPhase, ValidationResult

logger = logging.getLogger(__name__)


async def run_workflow(
    context: WorkflowContext,
    *,
    on_phase_change: object | None = None,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """运行外层 workflow 循环

    阶段流转：PLAN → CODE → VALIDATE → (replan | HUMAN_INTERVENE | DONE)

    Args:
        context: workflow 状态容器
        on_phase_change: 阶段变更回调

    Yields:
        AgentEvent 或 WorkflowPhase 变更通知
    """
    logger.info("[workflow] Starting workflow: task=%s", context.task[:80])

    while context.phase != WorkflowPhase.DONE:
        old_phase = context.phase

        if context.phase == WorkflowPhase.PLAN:
            async for event in _plan_phase(context):
                yield event

        elif context.phase == WorkflowPhase.CODE:
            async for event in _code_phase(context):
                yield event

        elif context.phase == WorkflowPhase.VALIDATE:
            async for event in _validate_phase(context):
                yield event

        elif context.phase == WorkflowPhase.HUMAN_INTERVENE:
            async for event in _human_intervene_phase(context):
                yield event

        if context.phase != old_phase:
            logger.info("[workflow] Phase: %s → %s", old_phase.value, context.phase.value)
            yield context.phase

    logger.info("[workflow] Workflow done: phase=%s", context.phase.value)


async def _plan_phase(ctx: WorkflowContext) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """PLAN 阶段：生成执行计划"""
    logger.info("[workflow] PLAN phase: generating plan...")
    # TODO: 调用 inner agent loop 生成计划
    # 暂时直接进入 CODE 阶段
    ctx.plan = f"Plan for: {ctx.task}"
    ctx.phase = WorkflowPhase.CODE
    yield ctx.phase


async def _code_phase(ctx: WorkflowContext) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """CODE 阶段：执行编码任务"""
    logger.info("[workflow] CODE phase: executing plan...")
    # TODO: 调用 inner agent loop 执行编码
    ctx.phase = WorkflowPhase.VALIDATE
    yield ctx.phase


async def _validate_phase(ctx: WorkflowContext) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """VALIDATE 阶段：验证执行结果"""
    logger.info("[workflow] VALIDATE phase: checking results...")
    # TODO: 调用 inner agent loop 验证
    ctx.validate_result = ValidationResult(passed=True)
    ctx.phase = WorkflowPhase.DONE
    yield ctx.phase


async def _human_intervene_phase(ctx: WorkflowContext) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """HUMAN_INTERVENE 阶段：等待人工介入"""
    logger.info("[workflow] HUMAN_INTERVENE phase: waiting for human input...")
    # TODO: 等待人工输入
    ctx.phase = WorkflowPhase.DONE
    yield ctx.phase
