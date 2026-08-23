"""
压缩模块共享工具函数

提取自 l1_extract.py 和 l2_summarize.py 的重复代码。
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def messages_to_text(messages: list[Any]) -> str:
    """将消息列表转换为可读文本（用于 LLM 摘要/提取）

    Args:
        messages: 消息列表

    Returns:
        可读文本
    """
    parts = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            # 跳过 system message（通常是 prompt，不是对话内容）
            continue
        elif isinstance(msg, HumanMessage):
            content = extract_content(msg)
            if content:
                parts.append(f"User: {content}")
        elif isinstance(msg, AIMessage):
            content = extract_content(msg)
            tool_calls = getattr(msg, "tool_calls", None) or []
            if content:
                parts.append(f"Assistant: {content}")
            if tool_calls:
                for tc in tool_calls:
                    name = tc.get("name", "unknown")
                    args = tc.get("args", {})
                    parts.append(f"Assistant called tool: {name}({args})")
        elif isinstance(msg, ToolMessage):
            content = extract_content(msg)
            name = getattr(msg, "name", "unknown")
            if content:
                # 截断过长的工具输出
                if len(content) > 2000:
                    content = content[:2000] + "... [truncated]"
                parts.append(f"Tool [{name}]: {content}")
    return "\n".join(parts)


def extract_content(msg: Any) -> str:
    """提取消息内容（处理多模态格式）

    Args:
        msg: 消息对象

    Returns:
        消息内容文本
    """
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts)
    return str(content) if content else ""
