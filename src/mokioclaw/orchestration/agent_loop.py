"""
通用 Agent 工具调用循环

提供 run_agent_loop 函数，封装 "调用模型 → 检查工具调用 → 执行工具 → 循环" 的通用模式。
消除 code_agent、search_agent、planner_node、verifier_node 中的重复循环代码。
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, ToolMessage

from mokioclaw.core.utils import last_ai_content

# 工具执行器类型：接收一个 tool_call 字典，返回 ToolMessage
ToolExecutor = Callable[[dict[str, Any]], ToolMessage]


def run_agent_loop(
    model_with_tools: Any,
    messages: list[Any],
    *,
    tool_executor: ToolExecutor,
    max_loops: int = 8,
    stop_message: str = "stopped after the maximum tool loop count.",
) -> tuple[list[Any], list[dict[str, Any]]]:
    """执行通用的 Agent 工具调用循环

    Args:
        model_with_tools: 已绑定工具的 LangChain 模型
        messages: 初始消息列表（会原地追加）
        tool_executor: 工具执行回调，接收 tool_call dict，返回 ToolMessage
        max_loops: 最大循环次数
        stop_message: 达到最大循环时的提示信息

    Returns:
        (produced_messages, tool_events) 元组
        - produced_messages: 所有产生的消息
        - tool_events: 工具调用事件列表（由 tool_executor 的 writer 产生时为空，
          需要调用方自行收集）
    """
    produced_messages: list[Any] = []

    for _ in range(max_loops):
        response = model_with_tools.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = tool_executor(call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content=stop_message))

    return produced_messages
