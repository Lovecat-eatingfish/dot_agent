"""
dot.agent.history — repair_tool_history 自愈机制

每次发请求前修复历史中的孤儿 result、重复 result、错位 result。
即使 session 存储损坏或被手动编辑，agent 也能自愈。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dot.ai.types import AgentMessage, AssistantMessage, ToolResultMessage


@dataclass
class RepairResult:
    """修复结果"""
    messages: list[AgentMessage]
    repairs: list[str] = field(default_factory=list)


def repair_tool_history(messages: list[AgentMessage]) -> RepairResult:
    """修复消息历史中的工具调用完整性问题

    修复场景：
    1. 孤儿 result：有 ToolResultMessage 但没有对应的 AssistantMessage 中的 ToolCall
    2. 重复 result：同一个 tool_call_id 有多个 ToolResultMessage
    3. 错位 result：ToolResultMessage 在对应的 AssistantMessage 之前

    Args:
        messages: 原始消息列表

    Returns:
        RepairResult: 修复后的消息列表和修复说明
    """
    repairs: list[str] = []
    result_messages: list[ToolResultMessage] = []
    non_result_messages: list[AgentMessage] = []

    # 分离 tool result 和其他消息
    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            result_messages.append(msg)
        else:
            non_result_messages.append(msg)

    # 收集所有有效的 tool_call_id
    valid_call_ids: set[str] = set()
    for msg in non_result_messages:
        if isinstance(msg, AssistantMessage):
            for call in msg.tool_calls:
                valid_call_ids.add(call.id)

    # 过滤孤儿 result（没有对应的 tool call）
    filtered_results: list[ToolResultMessage] = []
    seen_call_ids: set[str] = set()
    for result in result_messages:
        if result.tool_call_id not in valid_call_ids:
            repairs.append(f"Removed orphan result for {result.tool_call_id}")
            continue
        if result.tool_call_id in seen_call_ids:
            repairs.append(f"Removed duplicate result for {result.tool_call_id}")
            continue
        seen_call_ids.add(result.tool_call_id)
        filtered_results.append(result)

    # 重新组装消息，保持原始顺序
    # 将 result 插回到对应 assistant message 之后
    final: list[AgentMessage] = []
    pending_results: dict[str, list[ToolResultMessage]] = {}
    for r in filtered_results:
        pending_results.setdefault(r.tool_call_id, []).append(r)

    used_call_ids: set[str] = set()
    for msg in non_result_messages:
        final.append(msg)
        if isinstance(msg, AssistantMessage):
            for call in msg.tool_calls:
                if call.id in pending_results and call.id not in used_call_ids:
                    for r in pending_results[call.id]:
                        final.append(r)
                    used_call_ids.add(call.id)

    # 追加剩余的 result（应该没有，但防御性处理）
    for r in filtered_results:
        if r.tool_call_id not in used_call_ids:
            final.append(r)
            repairs.append(f"Repositioned result for {r.tool_call_id}")

    return RepairResult(messages=final, repairs=repairs)
