"""
dot.coding.compress.l1_extract — L1 压缩（≥50%）

去掉可恢复的 tool 调用结果（如 read_file），截取少量内容 + 保留文件路径。
不需要 LLM 调用。
"""
from __future__ import annotations

from dot.ai.types import AgentMessage, AssistantMessage, ToolResultMessage, TextContent


# 可恢复的工具（结果可以从磁盘重新读取）
RECOVERABLE_TOOLS = {"read_file", "FileReadTool"}

# 截取保留的字符数
KEEP_CHARS = 200


def compact_l1(messages: list[AgentMessage]) -> list[AgentMessage]:
    """L1 压缩：去掉可恢复 tool 结果，保留路径引导 LLM 重新读取

    Args:
        messages: 原始消息列表

    Returns:
        压缩后的消息列表
    """
    result: list[AgentMessage] = []

    for msg in messages:
        if isinstance(msg, ToolResultMessage) and msg.tool_name in RECOVERABLE_TOOLS:
            # 截取少量内容 + 保留路径
            original_text = msg.text
            if len(original_text) > KEEP_CHARS:
                truncated = original_text[:KEEP_CHARS] + "..."
            else:
                truncated = original_text

            # 从 details 中提取路径
            path_hint = ""
            if isinstance(msg.details, dict):
                path_hint = msg.details.get("path", "")

            hint = f"[L1 compacted] Content truncated. File: {path_hint}. Use read_file to reload if needed.\n{truncated}"

            new_msg = ToolResultMessage(
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                content=[TextContent(text=hint)],
                is_error=msg.is_error,
            )
            result.append(new_msg)
        else:
            result.append(msg)

    return result
