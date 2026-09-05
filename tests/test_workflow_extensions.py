"""
tests/test_workflow_extensions — workflow 引擎扩展组件

覆盖：
- ParallelNode：并行聚合 / 空分支与重名分支拒绝 / 分支异常传播
- SubgraphNode：嵌套执行与结果合并 / data 共享 / 取消传播 / 失败转 WorkflowError
- workflow_name：context.to_report 正确携带图名
"""
from __future__ import annotations

import asyncio

from dot.workflow import (
    END,
    FunctionNode,
    ParallelBranch,
    ParallelNode,
    SubgraphNode,
    WorkflowContext,
    WorkflowGraph,
)


def _collect(graph: WorkflowGraph, ctx: WorkflowContext | None = None):
    async def _run():
        events = []
        async for event in graph.run(ctx):
            events.append(event)
        return events

    return asyncio.run(_run())


# ============================================================
# ParallelNode
# ============================================================

def test_parallel_node_fans_out_and_merges_results():
    async def slow_two(ctx):
        await asyncio.sleep(0.01)
        return 2

    node = ParallelNode("fan", (
        ParallelBranch("a", lambda ctx: 1),
        ParallelBranch("b", slow_two),
    ))
    graph = WorkflowGraph(name="p")
    graph.add_node(node)
    graph.add_node(FunctionNode("after", lambda ctx: ctx.get_result("fan")))
    graph.set_entry("fan")
    graph.add_edge("fan", "after")
    graph.add_edge("after", END)

    _collect(graph)
    # after 节点在 fan 完成后运行，读到的就是聚合结果
    ctx = WorkflowContext()
    _collect(graph, ctx)
    assert ctx.get_result("fan") == {"a": 1, "b": 2}
    assert ctx.get_result("after") == {"a": 1, "b": 2}
    assert ctx.status == "completed"


def test_parallel_node_rejects_no_or_duplicate_branches():
    node = ParallelNode("fan", ())
    graph = WorkflowGraph(name="p")
    graph.add_node(node)
    graph.set_entry("fan")
    # 节点内异常不抛出，而是转为 WorkflowErrorEvent 终止
    events = _collect(graph)
    assert events[-1].type == "workflow_error"
    assert "no branches" in events[-1].error

    dup = ParallelNode("fan2", (
        ParallelBranch("x", lambda ctx: 1),
        ParallelBranch("x", lambda ctx: 2),
    ))
    graph2 = WorkflowGraph(name="p2")
    graph2.add_node(dup)
    graph2.set_entry("fan2")
    events2 = _collect(graph2)
    assert events2[-1].type == "workflow_error"
    assert "duplicate branch names" in events2[-1].error


def test_parallel_node_branch_failure_cancels_siblings_and_fails():
    started = []

    async def boom(ctx):
        started.append("boom")
        raise RuntimeError("branch failed")

    async def slow_ok(ctx):
        started.append("slow")
        await asyncio.sleep(5)
        return "never"

    node = ParallelNode("fan", (
        ParallelBranch("boom", boom),
        ParallelBranch("slow", slow_ok),
    ))
    graph = WorkflowGraph(name="p")
    graph.add_node(node)
    graph.set_entry("fan")

    events = _collect(graph)
    assert graph  # 事件流以 error 结束
    assert events[-1].type == "workflow_error"
    assert "branch failed" in events[-1].error
    # slow 分支被取消，不会跑满 5 秒（collect 在其完成前返回）


# ============================================================
# SubgraphNode
# ============================================================

def _build_inner_graph():
    inner = WorkflowGraph(name="inner")
    inner.add_node(FunctionNode("double", lambda ctx: ctx.get_result("ten") * 2))
    inner.set_entry("double")
    inner.add_edge("double", END)
    return inner


def test_subgraph_runs_nested_and_merges_results():
    outer = WorkflowGraph(name="outer")
    outer.add_node(FunctionNode("ten", lambda ctx: 10))
    outer.add_node(SubgraphNode("sub", _build_inner_graph()))
    outer.add_node(FunctionNode("after", lambda ctx: ctx.get_result("double")))
    outer.set_entry("ten")
    outer.add_edge("ten", "sub")
    outer.add_edge("sub", "after")
    outer.add_edge("after", END)

    ctx = WorkflowContext()
    events = _collect(outer, ctx)

    # 子图 results 合并回父 ctx；后续节点能读到子图产物
    assert ctx.get_result("double") == 20
    assert ctx.get_result("after") == 20
    assert ctx.status == "completed"
    # 子图的 NodeStart 事件透传到父事件流
    assert any(e.type == "workflow_node_start" and e.node == "double" for e in events)
    # to_report 带图名（workflow_name 修复验证）
    assert ctx.to_report()["workflow_name"] == "outer"


def test_subgraph_shares_data_and_cancellation():
    inner = WorkflowGraph(name="inner")
    seen = {}

    def read_data(ctx):
        seen["value"] = ctx.data.get("k")
        ctx.signal.cancel()  # 子图节点内部取消 → 父图也应感知
        return 1

    inner.add_node(FunctionNode("n", read_data))
    inner.set_entry("n")
    inner.add_edge("n", END)

    outer = WorkflowGraph(name="outer")
    outer.add_node(SubgraphNode("sub", inner))
    outer.add_node(FunctionNode("next", lambda ctx: 2))
    outer.set_entry("sub")
    outer.add_edge("sub", "next")

    ctx = WorkflowContext()
    ctx.data["k"] = "shared"
    events = _collect(outer, ctx)

    assert seen["value"] == "shared"        # data 按引用共享
    assert ctx.signal.is_cancelled()        # 取消令牌是同一个实例
    assert ctx.status == "cancelled"        # 父图在下一节点前停止
    assert not any(e.node == "next" and e.type == "workflow_node_end" for e in events)


def test_subgraph_failure_raises_workflow_error_to_parent():
    inner = WorkflowGraph(name="inner")

    def boom(ctx):
        raise RuntimeError("inner exploded")

    inner.add_node(FunctionNode("n", boom))
    inner.set_entry("n")
    inner.add_edge("n", END)

    outer = WorkflowGraph(name="outer")
    outer.add_node(SubgraphNode("sub", inner))
    outer.set_entry("sub")

    events = _collect(outer)
    last = events[-1]
    assert last.type == "workflow_error"
    assert "inner exploded" in last.error
