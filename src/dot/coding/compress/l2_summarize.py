"""
dot.coding.compress.l2_summarize — L2 压缩（≥70%）

删除老旧的 tool 调用（bash/grep 等输出），保留最近 N 轮不动。
不需要 LLM 调用。
"""
from __future__ import annotations

from dot.ai.types import AgentMessage, AssistantMessage, ToolResultMessage

# 保留最近 N 轮不动
KEEP_RECENT_TURNS = 3

# 可删除的工具输出
DELETABLE_TOOLS = {"bash", "grep", "glob_search", "BashTool", "GrepTool", "GlobTool"}


def compact_l2(messages: list[AgentMessage], *, keep_recent: int = KEEP_RECENT_TURNS) -> list[AgentMessage]:
    """L2 压缩：删除老旧 tool 调用输出，保留最近 N 轮

    Args:
        messages: 原始消息列表
        keep_recent: 保留最近的 turn 数

    Returns:
        压缩后的消息列表
    """
    # 找到最后 N 个 AssistantMessage 的位置（作为 turn 边界）
    assistant_indices = [
        i for i, msg in enumerate(messages) if isinstance(msg, AssistantMessage)
    ]

    if len(assistant_indices) <= keep_recent:
        return messages

    # 保留最近 keep_recent 个 turn
    cutoff_index = assistant_indices[-keep_recent]

    result: list[AgentMessage] = []
    for i, msg in enumerate(messages):
        if i >= cutoff_index:
            # 保留最近的 turn
            result.append(msg)
        elif isinstance(msg, ToolResultMessage) and msg.tool_name in DELETABLE_TOOLS:
            # 删除老旧的 tool 输出
            hint = f"[L2 compacted] Output removed ({msg.tool_name}). Re-run if needed."
            result.append(ToolResultMessage(
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                content=[],
                is_error=False,
            ))
        else:
            result.append(msg)

    return result
