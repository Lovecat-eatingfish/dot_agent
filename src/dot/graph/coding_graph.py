"""
LangGraph 图编排 — 状态 = Session（单 channel 包装）

设计（对齐 doc/fix.md，自定义机制，不过度依赖 langgraph）：
  - DotAgentState 只有一个 key："session"，值是 Session 对象本身
  - 节点通过 state["session"] 拿到同一个 Session 直接读写（Session IS State）
  - 消息手动 append（session.messages.append），不用 add_messages / RemoveMessage
  - 路由函数从 session 字段读取决策
  - langgraph 只用：StateGraph 节点/边/条件路由 + stream 事件流 +
    get_stream_writer；不用 interrupt / checkpointer / 回滚（全部自定义）
  - 人工介入：human_intervene 节点置 awaiting_intervention 后走 finally
    结束本轮（状态随 turn 持久化）；用户选 continue 由外部重新进图
  - 持久化在 finally 节点内完成：session.json + turn 快照 + agent 专用
    git commit（用户代码回滚）

节点：
  1. context_compress — 上下文压缩（超限裁剪，直接替换 session.messages）
  2. plan_node        — 生成 plan JSON
  3. coding_agent     — 执行编码（工具循环 max 10）
  4. valid_node       — 校验结果（工具循环 max 8）
  5. human_intervene  — 人工介入（置 awaiting 标记 → finally 结束本轮）
  6. finally_node     — 收尾 + 持久化（session.json / turn_xxx.json / git commit）
"""
from __future__ import annotations

from typing import Any, Required, TypedDict

from ..session.agent_context import AgentContext
from ..session.session import Session

from langgraph.graph import END, START, StateGraph

from ..compress.node import context_compress_node
from .helpers import _get_writer
from .nodes import (
    coding_agent_node,
    finally_node,
    human_intervene_node,
    plan_node,
    valid_node,
)
from .routing import route_coding_agent, route_valid_node


class DotAgentState(TypedDict, total=False):
    """图状态：双 channel

    - session:  会话状态（数据），图初始化时必填
    - context:  进程级组件（服务），可选

    finally_node 的输出通过 writer 发事件，不走 state channel。
    """
    session: Required[Session]
    context: AgentContext | None


# ============================================================
# Graph Builder
# ============================================================

def _traced_node(name: str, fn):
    """节点追踪包装：service=graph_node，自动挂 turn span 之下"""
    from ..trace import get_tracer

    def wrapper(state: DotAgentState) -> dict[str, Any]:
        session = state.get("session") if isinstance(state, dict) else None
        span = get_tracer().start_span(
            "graph_node", name,
            input_summary=(getattr(session, "task", "") or "")[:80],
        )
        try:
            result = fn(state)
            if isinstance(result, dict) and result:
                span.set_output_summary(f"keys={sorted(result.keys())[:5]}")
            else:
                span.set_output_summary("state-on-session")
            span.finish()
            return result
        except BaseException as exc:
            span.finish(exc)
            raise

    return wrapper


def build_graph() -> StateGraph:
    """构建未编译的图（无 checkpointer：介入/断点机制全部自定义）"""
    graph = StateGraph(DotAgentState)

    graph.add_node("context_compress", _traced_node("context_compress", context_compress_node))
    graph.add_node("plan_node", _traced_node("plan_node", plan_node))
    graph.add_node("coding_agent", _traced_node("coding_agent", coding_agent_node))
    graph.add_node("valid_node", _traced_node("valid_node", valid_node))
    graph.add_node("human_intervene", _traced_node("human_intervene", human_intervene_node))
    graph.add_node("finally_node", _traced_node("finally_node", finally_node))

    # 固定链路
    graph.add_edge(START, "context_compress")
    graph.add_edge("context_compress", "plan_node")
    graph.add_edge("plan_node", "coding_agent")
    graph.add_edge("human_intervene", "finally_node")
    graph.add_edge("finally_node", END)

    # 条件路由
    graph.add_conditional_edges(
        "coding_agent",
        route_coding_agent,
        {
            "plan_node": "plan_node",
            "valid_node": "valid_node",
            "human_intervene": "human_intervene",
        },
    )

    graph.add_conditional_edges(
        "valid_node",
        route_valid_node,
        {
            "finally_node": "finally_node",
            "coding_agent": "coding_agent",
            "human_intervene": "human_intervene",
        },
    )

    return graph


def compile_graph():
    """构建并编译图（无 checkpointer：断点/介入机制全部自定义）"""
    return build_graph().compile()
