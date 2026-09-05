"""
dot.coding.workflow — 内置 coding 工作流（plan → code → validate）

dot.workflow 通用引擎的一个业务实例，不再是引擎本身：
三个 AgentNode（plan / code / validate）+ validate 后的条件路由——
  验证通过            → END
  还有 replan 机会    → 回 plan
  replan 超限         → 按 ui_mode 进入 console / TUI 人工介入节点

所有阶段共享同一个 harness，消息历史跨节点累积；
阶段间数据通过 WorkflowContext（results / data）传递。
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from dot.agent.events import AgentEvent
from dot.agent.workflow import AgentNode
from dot.workflow import (
    END,
    FunctionNode,
    WorkflowContext,
    WorkflowInterruptEvent,
    WorkflowEvent,
    WorkflowGraph,
    run_with_interaction,
)
from .state import CodingWorkflowState, ValidationResult

if TYPE_CHECKING:
    from .host import CodingHost
    from dot.agent.harness import AgentHarness

logger = logging.getLogger(__name__)

# 业务状态在 WorkflowContext.data 中的挂载键
STATE_KEY = "coding"
HumanInterventionMode = Literal["none", "console", "tui"]
HumanInterventionHandler = Callable[[WorkflowInterruptEvent], bool | Awaitable[bool]]

# ============================================================
# 各节点 system prompt
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

End your reply with a final line: VERDICT: PASS or VERDICT: FAIL."""


# ============================================================
# 上下文 / 业务状态
# ============================================================

def create_context(task: str, *, max_replan: int = 3) -> WorkflowContext:
    """创建 coding 工作流的运行上下文"""
    if not task or not task.strip():
        raise ValueError("task must not be empty")
    if max_replan < 0:
        raise ValueError("max_replan must be non-negative")
    ctx = WorkflowContext()
    ctx.data[STATE_KEY] = CodingWorkflowState(task=task, max_replan=max_replan)
    return ctx


def get_state(ctx: WorkflowContext) -> CodingWorkflowState:
    """从上下文取业务状态（create_context 创建的 ctx 必有）"""
    return ctx.data[STATE_KEY]


# ============================================================
# 验证裁决解析
# ============================================================

_VERDICT_RE = re.compile(
    r"^\s*(?:\*\*)?\s*VERDICT\s*[:：]\s*(PASS|FAIL)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PASS_WORD_RE = re.compile(r"\bPASS(?:ED)?\b", re.IGNORECASE)
_FAIL_WORD_RE = re.compile(r"\bFAIL(?:ED|URE|URES)?\b", re.IGNORECASE)


def parse_verdict(text: str) -> ValidationResult:
    """解析验证节点输出

    优先匹配结构化标记 VERDICT: PASS/FAIL（末行约定），
    找不到时使用保守启发式：全文含 PASS 且没有失败措辞。
    """
    stripped = (text or "").strip()
    # 只接受独立的一行，并使用最后一个标记，避免正文中的示例/引用
    # 覆盖验证器最后给出的裁决。
    matches = list(_VERDICT_RE.finditer(stripped))
    match = matches[-1] if matches else None
    if match:
        passed = match.group(1).upper() == "PASS"
    else:
        upper = stripped.upper()
        # Without a structured marker, require a positive verdict and reject
        # any failure wording anywhere in the response.
        passed = bool(_PASS_WORD_RE.search(upper)) and not _FAIL_WORD_RE.search(upper)

    issues: list[str] = []
    if not passed:
        issues = [l for l in stripped.splitlines() if l.strip() and not l.lstrip().startswith("#")]

    return ValidationResult(passed=passed, message=stripped[:500], issues=issues)


# ============================================================
# 节点 prompt 工厂与回调
# ============================================================

def _plan_prompt(ctx: WorkflowContext) -> tuple[str, str | None]:
    state = get_state(ctx)
    system = PLAN_SYSTEM_PROMPT
    if state.validation_history:
        previous = state.validation_history[-1]
        feedback = previous.message or "No validation details were provided."
        if previous.issues:
            feedback += "\n\nIssues:\n" + "\n".join(
                f"- {issue}" for issue in previous.issues[:20]
            )
        system += (
            "\n\n## Previous Validation Feedback\n"
            "Revise the plan to address this failed validation:\n"
            f"{feedback}"
        )
    return state.task, system


def _plan_result(ctx: WorkflowContext, text: str) -> None:
    get_state(ctx).plan = text


def _code_prompt(ctx: WorkflowContext) -> tuple[str, str | None]:
    state = get_state(ctx)
    system = CODE_SYSTEM_PROMPT
    if state.plan:
        system += f"\n\n## Plan\n{state.plan}"
    return f"Execute the plan above for: {state.task}", system


def _validate_prompt(ctx: WorkflowContext) -> tuple[str, str | None]:
    state = get_state(ctx)
    system = VALIDATE_SYSTEM_PROMPT
    if state.plan:
        system += f"\n\n## Original Plan\n{state.plan}"
    system += f"\n\n## Task\n{state.task}"
    return "Verify the work product.", system


def _validate_router(
    ctx: WorkflowContext,
    human_intervention_node: str = "human_intervene",
) -> str:
    """validate 后的路由：解析裁决并决定 通过/回炉/终止"""
    state = get_state(ctx)
    state.validate_result = parse_verdict(ctx.get_result("validate") or "")
    state.validation_history.append(state.validate_result)

    if state.validate_result.passed:
        return END
    if state.should_replan():
        state.mark_replan()
        logger.info("[workflow] replan %d/%d", state.replan_count, state.max_replan)
        return "plan"

    state.error = f"Validation failed after {state.max_replan} replans"
    return human_intervention_node


async def _human_intervene_fn(ctx: WorkflowContext) -> None:
    """通过通用 interrupt/resume 等待人工决策。"""
    state = get_state(ctx)
    reason = state.error or "validation failed"
    result = state.validate_result
    decision = await ctx.interrupt(
        f"Validation failed after {state.max_replan} replans: {reason}",
        payload={
            "task": state.task,
            "issues": result.issues if result is not None else [],
        },
    )
    if not decision:
        raise RuntimeError(f"human intervention rejected: {reason}")
    state.continue_after_human_intervention()


def console_interaction(event: WorkflowInterruptEvent) -> bool:
    """Default console interaction handler for workflow interrupts."""
    print(f"\n[workflow] {event.reason}")
    if event.payload:
        for k, v in event.payload.items():
            print(f"  {k}: {v}")
    try:
        answer = input("Continue? [y/N] ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _intervention_node_name(mode: HumanInterventionMode) -> str:
    if mode == "console":
        return "human_intervene_console"
    if mode == "tui":
        return "human_intervene_tui"
    return "human_intervene"


# ============================================================
# 图定义与入口
# ============================================================

def build_coding_workflow(
    host: "CodingHost",
    ctx: WorkflowContext,
    *,
    max_turns: int | None = 30,
    ui_mode: HumanInterventionMode = "none",
    harness: "AgentHarness | None" = None,
) -> WorkflowGraph:
    """用通用引擎组装 plan→code→validate 工作流"""
    if ui_mode not in {"none", "console", "tui"}:
        raise ValueError("ui_mode must be 'none', 'console', or 'tui'")
    state = get_state(ctx)
    if harness is None:
        harness = host.create_harness(max_turns=max_turns)
    elif hasattr(harness, "config"):
        # TUI 复用当前 harness 时，仍要让本次 workflow 的限制生效。
        harness.config.max_turns = max_turns

    # 人工确认后可以重新获得预算，因此允许多次人工介入；仍用上限
    # 防止回调持续选择 continue 时无限运行。
    max_steps = max(100, 4 * (state.max_replan + 1) + 4)
    graph = WorkflowGraph(name="coding", max_steps=max_steps)
    human_node = _intervention_node_name(ui_mode)

    graph.add_node(AgentNode("plan", harness, _plan_prompt, on_result=_plan_result))
    graph.add_node(AgentNode("code", harness, _code_prompt))
    graph.add_node(AgentNode("validate", harness, _validate_prompt))
    graph.add_node(FunctionNode(human_node, _human_intervene_fn, store_result=False))

    graph.set_entry("plan")
    graph.add_edge("plan", "code")
    graph.add_edge("code", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda router_ctx: _validate_router(router_ctx, human_node),
    )
    graph.add_edge(human_node, "plan")
    return graph


async def run_workflow(
    ctx: WorkflowContext,
    host: "CodingHost",
    *,
    max_turns: int | None = 30,
    ui_mode: HumanInterventionMode = "none",
    human_intervene: HumanInterventionHandler | None = None,
    harness: "AgentHarness | None" = None,
) -> AsyncIterator[AgentEvent | WorkflowEvent]:
    """运行 coding 工作流（引擎事件 + Agent 事件原样透传）"""
    graph = build_coding_workflow(
        host,
        ctx,
        max_turns=max_turns,
        ui_mode=ui_mode,
        harness=harness,
    )
    async def _reject_intervention(_: WorkflowInterruptEvent) -> bool:
        logger.warning("[workflow] HUMAN_INTERVENE unavailable")
        return False

    handler = human_intervene
    if handler is None and ui_mode == "console":
        handler = console_interaction
    if handler is None:
        handler = _reject_intervention

    async for event in run_with_interaction(graph, ctx, handler):
        yield event


# ============================================================
# 统一回合执行器（console / one-shot CLI / TUI 共用）
# ============================================================

@dataclass
class WorkflowTurn:
    """一次 workflow 回合：上下文 + 事件流

    context 供调用方读取业务状态（如 validate_result），
    events 是 create_context → run_workflow 的事件迭代器。
    """
    context: WorkflowContext
    events: AsyncIterator[AgentEvent | WorkflowEvent]


def start_workflow_turn(
    host: "CodingHost",
    task: str,
    *,
    max_replan: int = 3,
    max_turns: int | None = 30,
    ui_mode: HumanInterventionMode = "console",
    human_intervene: HumanInterventionHandler | None = None,
    harness: "AgentHarness | None" = None,
) -> WorkflowTurn:
    """创建并启动一次 coding workflow 回合（不开始迭代）

    三处调用方（console REPL / agent run 一次性任务 / TUI）原先各自
    重复 create_context → run_workflow 装配，收敛到这里。
    """
    context = create_context(task, max_replan=max_replan)

    async def _events() -> AsyncIterator[AgentEvent | WorkflowEvent]:
        async for event in run_workflow(
                context,
                host,
                max_turns=max_turns,
                ui_mode=ui_mode,
                human_intervene=human_intervene,
                harness=harness,
        ):
            yield event

    return WorkflowTurn(context=context, events=_events())
