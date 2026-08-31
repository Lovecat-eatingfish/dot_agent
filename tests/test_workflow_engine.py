# dot.workflow 引擎测试 — 图定义 / 路由 / 兜底 / 取消
#
# 不依赖真实 LLM：AgentNode 走 harness，这里用实现 WorkflowNode 协议的
# FakeNode 验证事件透传与编排语义。
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from dot.workflow import (
    END,
    FunctionCompensationNode,
    FunctionNode,
    WorkflowContext,
    WorkflowDoneEvent,
    WorkflowErrorEvent,
    WorkflowGraph,
    WorkflowInterruptEvent,
    WorkflowNodeEndEvent,
    WorkflowNodeStartEvent,
    WorkflowValidationError,
)


def run_graph(graph: WorkflowGraph, ctx: WorkflowContext | None = None):
    """收集一次运行的 (events, ctx)"""
    async def _run():
        events = []
        async for event in graph.run(ctx):
            events.append(event)
        return events

    return asyncio.run(_run())


# ============================================================
# 定义期校验
# ============================================================

def test_duplicate_node_rejected():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    with pytest.raises(ValueError, match="duplicate"):
        graph.add_node(FunctionNode("a", lambda ctx: None))


def test_edge_to_unknown_node_rejected():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    with pytest.raises(ValueError, match="unknown node"):
        graph.add_edge("a", "missing")


def test_router_from_unknown_node_rejected():
    graph = WorkflowGraph()
    with pytest.raises(ValueError, match="unknown node"):
        graph.add_conditional_edges("missing", lambda ctx: END)


def test_node_with_two_outgoing_edges_rejected():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    graph.add_node(FunctionNode("b", lambda ctx: None))
    graph.add_node(FunctionNode("c", lambda ctx: None))
    graph.add_edge("a", "b")
    with pytest.raises(ValueError, match="outgoing"):
        graph.add_edge("a", "c")


def test_entry_required():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    with pytest.raises(WorkflowValidationError, match="entry"):
        run_graph(graph)


def test_unreachable_nodes_detected():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    graph.add_node(FunctionNode("b", lambda ctx: None))  # 不可达
    graph.set_entry("a")
    graph.add_edge("a", END)
    with pytest.raises(WorkflowValidationError, match="unreachable"):
        graph.validate()


def test_cycle_detected():
    graph = WorkflowGraph(max_steps=200)
    graph.add_node(FunctionNode("a", lambda ctx: None))
    graph.add_node(FunctionNode("b", lambda ctx: None))
    graph.add_node(FunctionNode("c", lambda ctx: None))
    graph.set_entry("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", "a")  # 形成环路
    with pytest.raises(WorkflowValidationError, match="cycle"):
        graph.validate()


# ============================================================
# 执行语义
# ============================================================

def test_linear_flow_passes_results():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: 1))
    graph.add_node(FunctionNode("b", lambda ctx: ctx.get_result("a") + 1))
    graph.set_entry("a")
    graph.add_edge("a", "b")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)

    assert ctx.results == {"a": 1, "b": 2}
    assert [type(e).__name__ for e in events] == [
        "WorkflowNodeStartEvent", "WorkflowNodeEndEvent",
        "WorkflowNodeStartEvent", "WorkflowNodeEndEvent",
        "WorkflowDoneEvent",
    ]


def test_async_function_node():
    async def async_fn(ctx: WorkflowContext) -> str:
        return "async-ok"

    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", async_fn))
    graph.set_entry("a")

    ctx = WorkflowContext()
    run_graph(graph, ctx)
    assert ctx.get_result("a") == "async-ok"


def test_conditional_routing_by_context():
    def router(ctx: WorkflowContext) -> str:
        return "big" if ctx.data["n"] > 10 else "small"

    graph = WorkflowGraph()
    graph.add_node(FunctionNode("pick", lambda ctx: None, store_result=False))
    graph.add_node(FunctionNode("big", lambda ctx: "B"))
    graph.add_node(FunctionNode("small", lambda ctx: "S"))
    graph.set_entry("pick")
    graph.add_conditional_edges("pick", router)

    ctx = WorkflowContext(data={"n": 42})
    run_graph(graph, ctx)
    assert ctx.get_result("big") == "B"
    assert "small" not in ctx.results

    ctx2 = WorkflowContext(data={"n": 3})
    run_graph(graph, ctx2)
    assert ctx2.get_result("small") == "S"


def test_router_can_end_workflow():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: "done"))
    graph.set_entry("a")
    graph.add_conditional_edges("a", lambda ctx: END)

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert ctx.get_result("a") == "done"
    assert isinstance(events[-1], WorkflowDoneEvent)


def test_static_edge_can_end_workflow():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: "done"))
    graph.set_entry("a")
    graph.add_edge("a", END)

    events = run_graph(graph)
    assert isinstance(events[-1], WorkflowDoneEvent)


def test_invalid_max_steps_rejected():
    with pytest.raises(ValueError, match="max_steps"):
        WorkflowGraph(max_steps=0)


def test_router_returning_unknown_node_fails_cleanly():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: None))
    graph.set_entry("a")
    graph.add_conditional_edges("a", lambda ctx: "ghost")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "unknown node" in ctx.error


def test_max_steps_guards_infinite_loop():
    graph = WorkflowGraph(max_steps=5)
    graph.add_node(FunctionNode("x", lambda ctx: None, store_result=False))
    graph.set_entry("x")
    graph.add_conditional_edges("x", lambda ctx: "x")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "max_steps" in ctx.error


def test_node_exception_terminates_with_error_event():
    def boom(ctx: WorkflowContext) -> None:
        raise RuntimeError("boom")

    graph = WorkflowGraph()
    graph.add_node(FunctionNode("ok", lambda ctx: 1))
    graph.add_node(FunctionNode("bad", boom))
    graph.set_entry("ok")
    graph.add_edge("ok", "bad")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)

    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "RuntimeError: boom" in ctx.error
    ends = [e for e in events if isinstance(e, WorkflowNodeEndEvent)]
    assert ends[-1].ok is False
    assert "boom" in ends[-1].error


def test_cancellation_stops_before_next_node():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: 1))
    graph.add_node(FunctionNode("b", lambda ctx: 2))
    graph.set_entry("a")
    graph.add_edge("a", "b")

    ctx = WorkflowContext()
    ctx.signal.cancel()
    events = run_graph(graph, ctx)

    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "cancelled" in ctx.error
    assert ctx.results == {}  # a 都没执行


def test_custom_node_events_are_forwarded():
    class FakeNode:
        name = "fake"

        async def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]:
            yield "inner-item"
            yield WorkflowNodeStartEvent(node="inner")

    graph = WorkflowGraph()
    graph.add_node(FakeNode())
    graph.set_entry("fake")

    events = run_graph(graph)
    # 引擎事件与节点透传项交错，透传不做包装
    assert events[0] == "inner-item" or isinstance(events[0], WorkflowNodeStartEvent)
    assert "inner-item" in events
    assert isinstance(events[-1], WorkflowDoneEvent)


def test_interrupt_event_pauses_until_context_is_resumed():
    async def approval_node(ctx: WorkflowContext):
        decision = await ctx.interrupt(
            "approve deployment",
            payload={"environment": "test"},
        )
        ctx.set_result("approval", decision)

    graph = WorkflowGraph(name="interrupt-test")
    graph.add_node(FunctionNode("approval", approval_node, store_result=False))
    graph.set_entry("approval")

    ctx = WorkflowContext()

    async def _run():
        events = []
        async for event in graph.run(ctx):
            events.append(event)
            if isinstance(event, WorkflowInterruptEvent):
                assert ctx.status == "paused"
                assert event.node == "approval"
                assert event.payload == {"environment": "test"}
                assert ctx.resume("approved", interrupt_id=event.interrupt_id)
        return events

    events = asyncio.run(_run())
    assert ctx.status == "completed"
    assert ctx.current_node is None
    assert ctx.completed_nodes == ["approval"]
    assert ctx.get_result("approval") == "approved"
    assert isinstance(events[-1], WorkflowDoneEvent)
    assert events[-1].run_id == ctx.run_id


def test_node_retry_with_backoff():
    attempts = 0

    def flaky(ctx: WorkflowContext) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return "ok"

    graph = WorkflowGraph()
    graph.add_node(
        FunctionNode("flaky", flaky),
        retries=2,
        backoff_base=0.01,  # 快速退避用于测试
        backoff_max=0.1,
    )
    graph.set_entry("flaky")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert attempts == 2
    end = [e for e in events if isinstance(e, WorkflowNodeEndEvent) and e.node == "flaky"]
    assert end[-1].attempts == 2
    assert ctx.status == "completed"


def test_node_retry_and_timeout_are_reported():
    attempts = 0

    def flaky(ctx: WorkflowContext) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return "ok"

    async def slow_node(ctx: WorkflowContext) -> None:
        await asyncio.sleep(1)

    graph = WorkflowGraph()
    graph.add_node(
        FunctionNode("flaky", flaky),
        retries=1,
        backoff_base=0.001,
    )
    graph.add_node(FunctionNode("slow", slow_node), timeout=0.01)
    graph.set_entry("flaky")
    graph.add_edge("flaky", "slow")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert attempts == 2
    end = [e for e in events if isinstance(e, WorkflowNodeEndEvent) and e.node == "flaky"]
    assert end[-1].attempts == 2
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert "timed out" in (ctx.error or "")
    assert ctx.status == "failed"


def test_compensation_node_executes_on_failure():
    compensated = []

    def compensate_fn(ctx: WorkflowContext, error: Exception) -> None:
        compensated.append(str(error))

    graph = WorkflowGraph()
    graph.add_node(
        FunctionNode("boom", lambda ctx: (_ for _ in ()).throw(RuntimeError("fail"))),
        compensate_with=FunctionCompensationNode("boom_comp", compensate_fn),
    )
    graph.set_entry("boom")

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)
    assert isinstance(events[-1], WorkflowErrorEvent)
    assert compensated == ["RuntimeError: fail"]


def test_graph_to_dict():
    graph = WorkflowGraph(name="test", max_steps=50)
    graph.add_node(FunctionNode("a", lambda ctx: 1), retries=2, timeout=30.0)
    graph.add_node(FunctionNode("b", lambda ctx: 2))
    graph.set_entry("a")
    graph.add_edge("a", "b")

    d = graph.to_dict()
    assert d["name"] == "test"
    assert d["max_steps"] == 50
    assert d["entry"] == "a"
    assert d["nodes"]["a"]["policy"]["retries"] == 2
    assert d["nodes"]["a"]["policy"]["timeout"] == 30.0
    assert d["edges"] == {"a": "b"}


def test_graph_to_mermaid():
    graph = WorkflowGraph(name="test")
    graph.add_node(FunctionNode("start", lambda ctx: None))
    graph.add_node(FunctionNode("end", lambda ctx: None))
    graph.set_entry("start")
    graph.add_edge("start", "end")

    mermaid = graph.to_mermaid()
    assert "flowchart LR" in mermaid
    assert "start" in mermaid
    assert "end" in mermaid
    assert "START" in mermaid


def test_events_have_step_and_duration():
    graph = WorkflowGraph()
    graph.add_node(FunctionNode("a", lambda ctx: "done"))
    graph.set_entry("a")
    graph.add_edge("a", END)

    ctx = WorkflowContext()
    events = run_graph(graph, ctx)

    start_event = next(e for e in events if isinstance(e, WorkflowNodeStartEvent))
    end_event = next(e for e in events if isinstance(e, WorkflowNodeEndEvent))
    done_event = next(e for e in events if isinstance(e, WorkflowDoneEvent))

    assert start_event.step == 1
    assert end_event.step == 1
    assert end_event.attempts == 1
    assert end_event.duration >= 0
    assert done_event.step == 1
    assert done_event.duration >= 0


def test_context_to_report():
    graph = WorkflowGraph(name="test-report")
    graph.add_node(FunctionNode("a", lambda ctx: 1))
    graph.set_entry("a")
    graph.add_edge("a", END)

    ctx = WorkflowContext()
    run_graph(graph, ctx)

    report = ctx.to_report()
    assert report["run_id"] is not None
    assert report["status"] == "completed"
    assert report["completed_nodes"] == ["a"]
    assert report["current_step"] == 1
    assert report["error"] is None
