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

from mokioclaw.orchestration.nodes import (
    chat_responder_node,
    code_agent_node,
    context_compressor_node,
    context_compressor_route,
    context_monitor_node,
    context_monitor_route,
    final_node,
    intent_route_fn,
    intent_router_node,
    planner_node,
    planner_route,
    repair_node,
    search_agent_node,
    verifier_node,
    verifier_route,
)
from mokioclaw.state.graph import MokioGraphState


def build_workflow():
    """构建默认工作流（当前等同于复杂工作流）"""
    return build_complex_workflow()


def build_complex_workflow():
    """构建复杂工作流，用于执行需要规划、验证的任务

    工作流图（轻量化 planner 版）：
    ┌──────────────────────────────────────────────────────────────────┐
    │ START                                                          │
    │   ↓                                                            │
    │ planner（规划器：生成计划 + 路由决策）                          │
    │   ↓                                                            │
    │ ┌──────────┬──────────┬──────────┬──────────┐                  │
    │ │ search   │ code     │ verify   │ final    │                  │
    │ │ agent    │ agent    │          │          │                  │
    │ └──────────┴──────────┴──────────┴──────────┘                  │
    │   ↓         ↓         ↓         ↓                              │
    │ context    context   (end)    (end)                            │
    │ monitor    monitor                                            │
    │   ↓         ↓                                                 │
    │ (route)    (route)                                            │
    │   ↓         ↓                                                 │
    │ compress   compress                                           │
    │ or verify  or verify                                          │
    │   ↓         ↓                                                 │
    │ verifier ←────── repair ←── verifier (failed)                 │
    │   ↓                                                            │
    │ final → END                                                    │
    └──────────────────────────────────────────────────────────────────┘

    Returns:
        编译后的 LangGraph 工作流
    """
    graph = StateGraph(MokioGraphState)

    # 添加节点
    graph.add_node("planner", planner_node)                    # 轻量化规划器：生成计划 + 路由决策
    graph.add_node("search_agent", search_agent_node)          # 搜索智能体
    graph.add_node("code_agent", code_agent_node)              # 代码智能体
    graph.add_node("context_monitor", context_monitor_node)    # 上下文监控
    graph.add_node("context_compressor", context_compressor_node)  # 上下文压缩
    graph.add_node("verifier", verifier_node)                  # 校验器
    graph.add_node("repair", repair_node)                      # 修复节点
    graph.add_node("final", final_node)                        # 结束节点

    # 定义边
    graph.add_edge(START, "planner")                           # 入口 → 规划器
    graph.add_conditional_edges(                               # 规划器 → 条件路由
        "planner",
        planner_route,
        {
            "search_agent": "search_agent",
            "code_agent": "code_agent",
            "verifier": "verifier",
            "final": "final",
            "planner": "planner",
        },
    )
    graph.add_edge("search_agent", "context_monitor")          # 搜索 → 上下文监控
    graph.add_edge("code_agent", "context_monitor")            # 代码 → 上下文监控
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
        {"verifier": "verifier", "planner": "planner"},
    )
    graph.add_conditional_edges(                               # 校验器 → 条件路由
        "verifier",
        verifier_route,
        {
            "final": "final",
            "repair": "repair",
            "planner": "planner",
        },
    )
    graph.add_edge("repair", "verifier")                       # 修复 → 重新校验
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
