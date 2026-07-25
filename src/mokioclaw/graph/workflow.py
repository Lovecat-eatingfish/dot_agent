"""
LangGraph 工作流定义模块

定义了 MokioClaw 的两个核心工作流：

1. Entry Workflow（入口工作流）：
   用户输入 → 意图路由 → 聊天回复 / 工作流

2. Complex Workflow（复杂工作流）：
   任务规划 → 上下文监控 → 校验 → 完成
              ↑              ↓
              └──── 重试 ────┘

工作流设计原则：
- 节点职责单一：每个节点只做一件事
- 状态驱动：节点间通过 MokioGraphState 传递数据
- 条件路由：根据状态决定下一步执行路径
- 容错重试：校验失败时自动重试（最多 max_attempts 次）
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from mokioclaw.graph.nodes import (
    chat_responder_node,
    context_compressor_node,
    context_compressor_route,
    context_monitor_node,
    context_monitor_route,
    final_node,
    intent_route_fn,
    intent_router_node,
    planner_node,
    verifier_node,
)
from mokioclaw.graph.state import MokioGraphState


def build_workflow():
    """构建默认工作流（当前等同于复杂工作流）"""
    return build_complex_workflow()


def build_complex_workflow():
    """构建复杂工作流，用于执行需要规划、验证的任务

    工作流图：
    ┌──────────────────────────────────────────────────────────┐
    │ START                                                    │
    │   ↓                                                      │
    │ planner（规划器）                                         │
    │   ↓                                                      │
    │ context_monitor（上下文监控）                              │
    │   ↓                                                      │
    │ ┌─────────────────────────────────────────────────────┐  │
    │ │ 条件路由：                                           │  │
    │ │   - context_compressor: 上下文过长，需要压缩         │  │
    │ │   - verifier: 正常流程，进入校验                     │  │
    │ │   - planner: 需要重新规划                            │  │
    │ │   - final: 任务完成                                  │  │
    │ └─────────────────────────────────────────────────────┘  │
    │   ↓                                                      │
    │ verifier（校验器）                                       │
    │   ↓                                                      │
    │ context_monitor（回到监控）                               │
    │   ↓                                                      │
    │ final（结束）→ END                                       │
    └──────────────────────────────────────────────────────────┘

    Returns:
        编译后的 LangGraph 工作流
    """
    graph = StateGraph(MokioGraphState)

    # 添加节点
    graph.add_node("planner", planner_node)                    # 规划器：制定计划、委派任务
    graph.add_node("context_monitor", context_monitor_node)    # 上下文监控：检查 token 数量
    graph.add_node("context_compressor", context_compressor_node)  # 上下文压缩：压缩过长的消息
    graph.add_node("verifier", verifier_node)                  # 校验器：验证任务完成情况
    graph.add_node("final", final_node)                        # 结束节点：生成最终结果

    # 定义边
    graph.add_edge(START, "planner")                           # 入口 → 规划器
    graph.add_edge("planner", "context_monitor")               # 规划器 → 上下文监控
    graph.add_conditional_edges(                               # 上下文监控 → 条件路由
        "context_monitor",
        context_monitor_route,
        {
            "context_compressor": "context_compressor",
            "verifier": "verifier",
            "planner": "planner",
            "final": "final",
        },
    )
    graph.add_conditional_edges(                               # 上下文压缩 → 条件路由
        "context_compressor",
        context_compressor_route,
        {"verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_edge("verifier", "context_monitor")              # 校验器 → 上下文监控（循环）
    graph.add_edge("final", END)                               # 结束 → 退出

    return graph.compile()


def build_entry_workflow():
    """构建入口工作流，用于判断用户意图并路由

    工作流图：
    ┌──────────────────────────────────────┐
    │ START                                │
    │   ↓                                  │
    │ intent_router（意图识别）             │
    │   ↓                                  │
    │ ┌─────────────────────────────────┐  │
    │ │ 条件路由：                       │  │
    │ │   - chat_responder: 轻量聊天     │  │
    │ │   - END → planner: 执行工作流    │  │
    │ └─────────────────────────────────┘  │
    │   ↓                                  │
    │ chat_responder（聊天回复）           │
    │   ↓                                  │
    │ END                                  │
    └──────────────────────────────────────┘

    Returns:
        编译后的 LangGraph 工作流
    """
    graph = StateGraph(MokioGraphState)

    # 添加节点
    graph.add_node("intent_router", intent_router_node)        # 意图路由器
    graph.add_node("chat_responder", chat_responder_node)      # 聊天回复器

    # 定义边
    graph.add_edge(START, "intent_router")                     # 入口 → 意图路由
    graph.add_conditional_edges(                               # 意图路由 → 条件路由
        "intent_router",
        intent_route_fn,
        {"chat_responder": "chat_responder", "planner": END},
    )
    graph.add_edge("chat_responder", END)                      # 聊天回复 → 结束

    return graph.compile()
