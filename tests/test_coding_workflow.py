"""dot.coding.workflow 测试 — plan→code→validate 图实例 / replan / VERDICT 解析

不依赖真实 LLM：FakeHarness 直接按脚本回复，FakeHost 只提供 create_harness。
语义参照遗留 tests/test_coding_graph.py 的路由断言。
"""
from __future__ import annotations

import asyncio

from dot.ai.types import AssistantMessage
from dot.agent.workflow import AgentNode
from dot.agent.events import MessageEndEvent
from dot.coding.state import ValidationResult
from dot.coding.workflow import (
    create_context,
    get_state,
    parse_verdict,
    run_workflow,
)
from dot.workflow import (
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowNodeStartEvent,
    WorkflowGraph,
    WorkflowContext,
)


# ============================================================
# Fakes
# ============================================================

class FakeHarness:
    """按脚本顺序回复的假 harness（记录每次 prompt 调用）"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str | None]] = []

    async def prompt(self, content: str, *, system: str | None = None):
        self.calls.append((content, system))
        reply = self.replies.pop(0) if self.replies else "VERDICT: PASS"
        yield MessageEndEvent(message=AssistantMessage(content=reply))


class FakeHost:
    """只实现 build_coding_workflow 需要的最小接口"""

    def __init__(self, harness: FakeHarness) -> None:
        self._harness = harness

    def create_harness(self, *, max_turns: int = 30) -> FakeHarness:
        return self._harness


def run_coding_workflow(replies: list[str], *, max_replan: int = 3):
    """跑一次 coding 工作流，返回 (events, ctx, harness)"""
    harness = FakeHarness(replies)
    host = FakeHost(harness)
    ctx = create_context("write a hello world", max_replan=max_replan)

    async def _run():
        events = []
        async for event in run_workflow(ctx, host):
            events.append(event)
        return events

    return asyncio.run(_run()), ctx, harness


# ============================================================
# 主干路径
# ============================================================

def test_happy_path_plan_code_validate():
    events, ctx, harness = run_coding_workflow([
        "1. write file",              # plan
        "wrote Hello.java",           # code
        "all good. VERDICT: PASS",    # validate
    ])

    state = get_state(ctx)
    assert state.plan == "1. write file"
    assert state.validate_result is not None
    assert state.validate_result.passed is True
    assert state.replan_count == 0
    assert state.error is None
    assert ctx.get_result("code") == "wrote Hello.java"
    assert isinstance(events[-1], WorkflowDoneEvent)

    nodes = [e.node for e in events if isinstance(e, WorkflowNodeStartEvent)]
    assert nodes == ["plan", "code", "validate"]

    # 每个节点都有各自的 system prompt（system override 语义）
    systems = [system for _, system in harness.calls]
    assert systems[0].startswith("You are a planning agent")
    assert systems[1].startswith("You are a coding agent")
    assert "1. write file" in systems[1]  # plan 注入 code 阶段
    assert systems[2].startswith("You are a validation agent")
    assert systems[2].count("## Task") == 1


def test_build_workflow_passes_max_turns_to_harness():
    harness = FakeHarness([])
    ctx = create_context("task")

    from dot.coding.workflow import build_coding_workflow

    class RecordingHost:
        def create_harness(self, *, max_turns: int | None = 30) -> FakeHarness:
            self.max_turns = max_turns
            return harness

    host = RecordingHost()
    build_coding_workflow(host, ctx, max_turns=7)

    assert host.max_turns == 7


def test_replan_until_pass():
    # validate 先 FAIL 两次再 PASS
    events, ctx, _ = run_coding_workflow([
        "plan v1", "code v1", "VERDICT: FAIL",   # 第 1 轮
        "plan v2", "code v2", "VERDICT: FAIL",   # 第 2 轮（replan 1 次）
        "plan v3", "code v3", "VERDICT: PASS",   # 第 3 轮（replan 2 次）
    ])

    state = get_state(ctx)
    assert state.replan_count == 2
    assert state.validate_result.passed is True
    assert len(state.validation_history) == 3
    assert state.error is None

    nodes = [e.node for e in events if isinstance(e, WorkflowNodeStartEvent)]
    assert nodes == ["plan", "code", "validate"] * 3


def test_replan_prompt_contains_previous_validation_feedback():
    _, _, harness = run_coding_workflow([
        "plan v1", "code v1", "VERDICT: FAIL\nmissing test coverage",
        "plan v2", "code v2", "VERDICT: PASS",
    ])

    assert "Previous Validation Feedback" in (harness.calls[3][1] or "")
    assert "missing test coverage" in (harness.calls[3][1] or "")


def test_agent_error_terminates_workflow_at_failing_node():
    class ErrorHarness(FakeHarness):
        async def prompt(self, content: str, *, system: str | None = None):
            if "Execute the plan" in content:
                yield MessageEndEvent(message=AssistantMessage(
                    content=[], stop_reason="error", error_message="provider unavailable",
                ))
                return
            yield MessageEndEvent(message=AssistantMessage(content="plan"))

    harness = ErrorHarness([])
    ctx = create_context("task")

    async def _run():
        return [e async for e in run_workflow(ctx, FakeHost(harness))]

    events = asyncio.run(_run())
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "provider unavailable" in (ctx.error or "")
    assert [e.node for e in events if isinstance(e, WorkflowNodeStartEvent)] == ["plan", "code"]


def test_agent_node_propagates_workflow_cancellation():
    class CancellableHarness:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        async def prompt(self, content: str, *, system: str | None = None):
            del content, system
            self.started.set()
            while not self.cancelled:
                await asyncio.sleep(0.01)
            yield MessageEndEvent(message=AssistantMessage(
                content=[], stop_reason="aborted", error_message="cancelled",
            ))

    harness = CancellableHarness()
    ctx = WorkflowContext()
    graph = WorkflowGraph()
    graph.add_node(AgentNode("agent", harness, lambda _: ("run", None)))
    graph.set_entry("agent")

    async def _run():
        events = []
        async for event in graph.run(ctx):
            events.append(event)
            if isinstance(event, WorkflowNodeStartEvent):
                ctx.signal.cancel()
        return events

    events = asyncio.run(_run())
    assert harness.cancelled is True
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "cancelled" in (ctx.error or "")


def test_replan_exhausted_goes_to_human_intervene():
    events, ctx, _ = run_coding_workflow(
        ["plan", "code", "VERDICT: FAIL"] * 2,  # max_replan=1：第 2 次 FAIL 后无预算
        max_replan=1,
    )

    state = get_state(ctx)
    assert state.validate_result.passed is False
    assert "Validation failed after 1 replans" in state.error

    nodes = [e.node for e in events if isinstance(e, WorkflowNodeStartEvent)]
    assert nodes == ["plan", "code", "validate", "plan", "code", "validate", "human_intervene"]
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "human intervention rejected" in (ctx.error or "")


def test_tui_human_intervention_can_continue_with_new_budget():
    harness = FakeHarness([
        "plan v1", "code v1", "VERDICT: FAIL",
        "plan v2", "code v2", "VERDICT: FAIL",
        "plan v3", "code v3", "VERDICT: PASS",
    ])
    ctx = create_context("task", max_replan=1)
    decisions = iter([True])

    async def _run():
        return [e async for e in run_workflow(
            ctx,
            FakeHost(harness),
            ui_mode="tui",
            human_intervene=lambda _: next(decisions),
        )]

    events = asyncio.run(_run())
    state = get_state(ctx)
    assert state.validate_result is not None and state.validate_result.passed
    assert state.human_intervention_count == 1
    assert state.replan_count == 0
    assert isinstance(events[-1], WorkflowDoneEvent)
    assert [e.node for e in events if isinstance(e, WorkflowNodeStartEvent)] == [
        "plan", "code", "validate",
        "plan", "code", "validate",
        "human_intervene_tui",
        "plan", "code", "validate",
    ]


def test_console_human_intervention_rejection_is_an_error():
    harness = FakeHarness([
        "plan", "code", "VERDICT: FAIL",
        "plan", "code", "VERDICT: FAIL",
    ])
    ctx = create_context("task", max_replan=1)

    async def _run():
        return [e async for e in run_workflow(
            ctx,
            FakeHost(harness),
            ui_mode="console",
            human_intervene=lambda _: False,
        )]

    events = asyncio.run(_run())
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "human intervention rejected" in (ctx.error or "")
    assert any(
        isinstance(e, WorkflowNodeStartEvent) and e.node == "human_intervene_console"
        for e in events
    )


def test_engine_error_event_on_node_crash():
    # harness 在第 3 次调用后不再产出（replies 为空时 FakeHarness 兜底，
    # 这里用非法输入模拟：直接让 prompt 抛异常）
    class BoomHarness(FakeHarness):
        async def prompt(self, content: str, *, system: str | None = None):
            if "Execute the plan" in content:
                raise RuntimeError("llm down")
            yield MessageEndEvent(message=AssistantMessage(content="plan"))

    harness = BoomHarness([])
    host = FakeHost(harness)
    ctx = create_context("task")

    async def _run():
        return [e async for e in run_workflow(ctx, host)]

    events = asyncio.run(_run())
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "RuntimeError: llm down" in ctx.error
    assert ctx.error is not None


# ============================================================
# parse_verdict
# ============================================================

def test_parse_verdict_structured_marker():
    assert parse_verdict("All checks done.\nVERDICT: PASS").passed is True
    assert parse_verdict("verdict: pass").passed is True          # 大小写不敏感
    assert parse_verdict("**VERDICT: FAIL**\n- bug in main()").passed is False
    assert parse_verdict("VERDICT：FAIL").passed is False          # 全角冒号


def test_parse_verdict_uses_last_standalone_marker():
    text = "Example: VERDICT: PASS\nChecks found a problem.\n**VERDICT: FAIL**"
    assert parse_verdict(text).passed is False


def test_create_context_rejects_invalid_limits():
    import pytest

    with pytest.raises(ValueError, match="task"):
        create_context(" ")
    with pytest.raises(ValueError, match="max_replan"):
        create_context("task", max_replan=-1)


def test_parse_verdict_fails_when_fail_mentioned():
    # FAIL 在文本中但无结构化标记 → 启发式回退：开头含 FAIL 判不通过
    result = parse_verdict("FAIL: compilation error\nat line 3")
    assert result.passed is False
    assert result.issues


def test_parse_verdict_fallback_heuristic():
    # 无标记：含 PASS 且开头无 FAIL → 通过（与旧逻辑一致）
    assert parse_verdict("Everything looks PASS to me").passed is True
    # 无标记也无 PASS → 不通过
    assert parse_verdict("cannot verify anything").passed is False


def test_parse_verdict_rejects_failure_later_in_response():
    result = parse_verdict("Most checks PASS. However, integration tests FAIL.")
    assert result.passed is False


def test_parse_verdict_ignores_fail_in_details():
    # "PASS" 裁决 + 正文提到一次早先的 FAIL —— 结构化标记应压过正文
    text = "An earlier step failed but was fixed.\nVERDICT: PASS"
    assert parse_verdict(text).passed is True


def test_validation_result_shape():
    result: ValidationResult = parse_verdict("VERDICT: FAIL\n# header\nissue one\nissue two")
    assert result.passed is False
    assert "issue one" in result.issues
    assert "# header" not in result.issues
