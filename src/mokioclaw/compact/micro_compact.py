"""
MicroCompact 层：大工具输出占位替换（无 LLM，每轮必执行）
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import ToolMessage

from mokioclaw.compact.types import CompactConfig, CompactState
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

_PLACEHOLDER = "[Tool result compacted: original stored in transcript]"
_RECENT_RETAIN = 3  # 最近 2-3 次工具输出完整保留
_LARGE_THRESHOLD = 2000  # 超过此字符数视为大输出
_LARGE_OUTPUT_TOOLS = {"read_file", "BashTool", "grep", "glob", "search"}


def micro_compact(messages: list[Any], config: CompactConfig, state: CompactState) -> list[Any]:
    """处理高输出工具的占位替换。

    规则：
    1. 最近 2-3 次工具输出完整保留，不占位
    2. 热缓存模式：仅 metadata 打标记，不修改 content，保护前缀缓存
    3. 冷缓存模式：tool_result 替换为占位文本
    4. 图片内容替换为占位标记
    """
    if not messages:
        return messages

    result: list[Any] = []
    tool_output_count = 0

    # 统计总工具输出数，确定哪些是"最近"
    total_tool_outputs = sum(1 for m in messages if isinstance(m, ToolMessage))
    recent_threshold = max(0, total_tool_outputs - _RECENT_RETAIN)

    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_output_count += 1
            is_recent = tool_output_count > recent_threshold

            content = str(getattr(msg, "content", None) or "")
            name = getattr(msg, "name", None) or ""

            # 最近输出：完整保留
            if is_recent:
                result.append(msg)
                continue

            # 判断是否大输出
            is_large = len(content) > _LARGE_THRESHOLD or name in _LARGE_OUTPUT_TOOLS

            if is_large:
                # 图片内容替换
                if _is_image_content(content):
                    result.append(_make_placeholder(msg, "[Image content compacted]"))
                    continue

                # 冷缓存模式：替换为占位
                result.append(_make_placeholder(msg, _PLACEHOLDER))
                logger.debug("Micro-compacted tool output: %s", name)
            else:
                # 小输出直接保留
                result.append(msg)
        else:
            result.append(msg)

    return result


def _is_image_content(content: str) -> bool:
    """检测内容是否包含图片数据"""
    if not isinstance(content, str):
        return False
    markers = ["data:image/", "iVBOR", "/9j/", "iVBORw0KGgo"]
    return any(m in content for m in markers)


def _make_placeholder(original: ToolMessage, text: str) -> ToolMessage:
    """创建占位 ToolMessage，保留 tool_call_id 配对"""
    return ToolMessage(
        content=text,
        name=getattr(original, "name", None),
        id=getattr(original, "id", None),
        tool_call_id=getattr(original, "tool_call_id", None),
    )
