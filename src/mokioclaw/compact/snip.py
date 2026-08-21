"""
Snip 层：低价值回合过滤（无 LLM，纯规则）
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mokioclaw.compact.types import CompactConfig, CompactState
from mokioclaw.compact.compact_guard import is_protected
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


def _is_turn_start(idx: int, messages: list[Any]) -> bool:
    """新回合以 HumanMessage 或列表开头开始"""
    if idx == 0:
        return True
    return isinstance(messages[idx], HumanMessage)


def _classify_turn(messages: list[Any], start: int, end: int) -> str:
    """对单个回合分类: keep / snip / reject

    - keep: 保留完整回合
    - snip: 删除（空结果、无匹配等低价值回合）
    - reject: 用户拒绝了工具调用，保留为占位
    """
    turn_msgs = messages[start:end]

    for msg in turn_msgs:
        if isinstance(msg, ToolMessage):
            content = str(getattr(msg, "content", None) or "")
            name = getattr(msg, "name", None) or ""
            # 工具返回空结果
            if not content.strip():
                return "snip"
            # grep 无匹配、glob 无文件
            if name in ("grep", "glob"):
                lower = content.lower()
                if "no matches" in lower or "no files found" in lower or "nothing found" in lower:
                    return "snip"
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                args = tc.get("args", {}) or {}
                if isinstance(args, dict) and args.get("approve") is False:
                    return "reject"

    return "keep"


def snip_turns(messages: list[Any], config: CompactConfig, state: CompactState) -> list[Any]:
    """过滤低价值完整回合。

    规则：
    - 必须成对处理 tool_call + tool_result（按回合整体保留/删除）
    - 保护区内消息直接跳过
    - 过滤条件：工具返回空结果、grep/glob 无匹配、用户拒绝工具调用
    """
    if not messages or not config.enable_snip:
        return messages

    # 找回合边界
    boundaries = [0]
    for i in range(1, len(messages)):
        if _is_turn_start(i, messages):
            boundaries.append(i)
    boundaries.append(len(messages))

    result: list[Any] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        turn = messages[start:end]

        # 保护区直接跳过
        if any(is_protected(m) for m in turn):
            result.extend(turn)
            continue

        classification = _classify_turn(messages, start, end)
        if classification == "keep":
            result.extend(turn)
        elif classification == "reject":
            # 用户拒绝：保留一个占位
            result.append(HumanMessage(content="[用户拒绝了工具调用]"))
        else:
            # snip: 删除整回合
            logger.debug("Sniped turn [%d:%d]", start, end)

    return result
