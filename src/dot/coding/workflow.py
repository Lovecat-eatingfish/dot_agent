"""
dot.coding.workflow — run_workflow（外层循环）

普通 Python 代码 + 显式状态机（WorkflowPhase），
替代 LangGraph 的 StateGraph + 条件边。
阶段转换用 if/else，比 LangGraph 条件边直观。

外层 workflow 通过 CodingHost 创建 AgentHarness，调用内层 agent loop，
每个阶段通过函数返回值传递结果，不暴露内部状态。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dot.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from dot.agent.harness import AgentHarness
from dot.ai.types import AssistantMessage, TextContent

from .state import WorkflowContext, WorkflowPhase, ValidationResult

if TYPE_CHECKING:
    from .host import CodingHost

logger = logging.getLogger(__name__)

# ============================================================
# System prompts per phase
# ============================================================

PLAN_SYSTEM_PROMPT = """You are a planning agent. Given a task, produce a structured execution plan.

Output format:
## Goal
<one sentence>

## Steps
1. <step description>
2. <step description>
...

Be concise. Focus on what needs to be done, not how to do it."""

CODE_SYSTEM_PROMPT = """You are a coding agent. Execute the plan using available tools.

Available tools: read_file, write_file, edit_file, bash, glob_search, grep.
Always prefer targeted edits (edit_file) over full rewrites (write_file).
Report what you did after each significant step."""

VALIDATE_SYSTEM_PROMPT = """You are a validation agent. Verify the work product is correct and complete.

Check:
- Does the code compile/run without errors?
- Does it fulfill the original task?
- Are there obvious bugs or missing edge cases?

Respond with: PASS / FAIL + brief reason."""


# ============================================================
# run_workflow — 外层循环入口
# ============================================================

async def run_workflow(
    context: WorkflowContext,
    host: CodingHost,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """运行外层 workflow 循环

    阶段流转：PLAN → CODE → VALIDATE → (replan | HUMAN_INTERVENE | DONE)

    所有阶段共享同一个 harness，消息历史跨 phase 累积。

    Args:
        context: workflow 状态容器
        host: CodingHost（提供 tools / provider / permission）

    Yields:
        AgentEvent 或 WorkflowPhase 变更通知
    """
    logger.info("[workflow] Starting workflow: task=%s", context.task[:80])

    # 共享 harness：消息历史跨 phase 累积
    harness = host.create_harness(max_turns=30)

    while context.phase != WorkflowPhase.DONE:
        old_phase = context.phase

        if context.phase == WorkflowPhase.PLAN:
            async for event in _plan_phase(context, harness):
                yield event

        elif context.phase == WorkflowPhase.CODE:
            async for event in _code_phase(context, harness):
                yield event

        elif context.phase == WorkflowPhase.VALIDATE:
            async for event in _validate_phase(context, harness):
                yield event

        elif context.phase == WorkflowPhase.HUMAN_INTERVENE:
            async for event in _human_intervene_phase(context, harness):
                yield event

        if context.phase != old_phase:
            logger.info("[workflow] Phase: %s → %s", old_phase.value, context.phase.value)
            yield context.phase

    logger.info("[workflow] Workflow done: phase=%s", context.phase.value)


# ============================================================
# PLAN phase — 生成执行计划
# ============================================================

async def _plan_phase(
    ctx: WorkflowContext,
    harness: AgentHarness,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """PLAN 阶段：调用 inner agent loop 生成计划"""
    logger.info("[workflow] PLAN phase: generating plan...")

    final_message = ""
    async for event in harness.prompt(ctx.task, system=PLAN_SYSTEM_PROMPT):
        yield event
        if isinstance(event, (AgentStartEvent, AgentEndEvent, TurnStartEvent, TurnEndEvent, MessageStartEvent, MessageUpdateEvent, MessageEndEvent, ToolExecutionStartEvent, ToolExecutionEndEvent)) and hasattr(event, "message"):
            if isinstance(event, MessageEndEvent):
                msg = event.message
                if isinstance(msg, AssistantMessage) and msg.content:
                    for block in msg.content:
                        if isinstance(block, TextContent):
                            final_message += block.text

    ctx.plan = final_message.strip()
    ctx.phase = WorkflowPhase.CODE
    yield ctx.phase


# ============================================================
# CODE phase — 执行编码任务
# ============================================================

async def _code_phase(
    ctx: WorkflowContext,
    harness: AgentHarness,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """CODE 阶段：调用 inner agent loop 执行计划"""
    logger.info("[workflow] CODE phase: executing plan...")

    system = CODE_SYSTEM_PROMPT
    if ctx.plan:
        system += f"\n\n## Plan\n{ctx.plan}"

    task_msg = f"Execute the plan above for: {ctx.task}"
    async for event in harness.prompt(task_msg, system=system):
        yield event

    ctx.phase = WorkflowPhase.VALIDATE
    yield ctx.phase


# ============================================================
# VALIDATE phase — 验证执行结果
# ============================================================

async def _validate_phase(
    ctx: WorkflowContext,
    harness: AgentHarness,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """VALIDATE 阶段：调用 inner agent loop 验证结果"""
    logger.info("[workflow] VALIDATE phase: checking results...")

    system = VALIDATE_SYSTEM_PROMPT
    if ctx.plan:
        system += f"\n\n## Original Plan\n{ctx.plan}"
    system += f"\n\n## Task\n{ctx.task}"

    validation_response = ""
    async for event in harness.prompt("Verify the work product.", system=system):
        yield event
        if isinstance(event, MessageEndEvent):
            msg = event.message
            if isinstance(msg, AssistantMessage) and msg.content:
                for block in msg.content:
                    if isinstance(block, TextContent):
                        validation_response += block.text

    # 解析验证结果
    text = validation_response.strip().upper()
    passed = "PASS" in text and "FAIL" not in text[:10]
    issues = []
    if not passed:
        lines = validation_response.strip().splitlines()
        issues = [l for l in lines if l.strip() and not l.startswith("#")]

    ctx.validate_result = ValidationResult(
        passed=passed,
        message=validation_response[:500],
        issues=issues,
    )

    if passed:
        ctx.phase = WorkflowPhase.DONE
    elif ctx.should_replan():
        ctx.mark_replan()
    else:
        # 超过重试上限，标记失败
        ctx.mark_error(f"Validation failed after {ctx.max_replan} replans")

    yield ctx.phase


# ============================================================
# HUMAN_INTERVENE phase — 等待人工介入
# ============================================================

async def _human_intervene_phase(
    ctx: WorkflowContext,
    harness: AgentHarness,
) -> AsyncIterator[AgentEvent | WorkflowPhase]:
    """HUMAN_INTERVENE 阶段：等待人工确认后继续"""
    logger.info("[workflow] HUMAN_INTERVENE phase: waiting for human input...")

    # 目前为占位：记录问题后直接进 DONE
    # 后续可接入 TUI 审批流程
    ctx.validate_result = ValidationResult(
        passed=False,
        message="Human intervention required; automatically proceeding to DONE",
    )
    ctx.phase = WorkflowPhase.DONE
    yield ctx.phase
