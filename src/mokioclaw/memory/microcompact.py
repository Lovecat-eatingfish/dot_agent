"""
微压缩 / 过期工具结果清理

对齐 docs/agent.md：
- file_state_map: path → 最后一次写操作的 message_id
- 若某条 FileReadTool 的 message_id < 该文件最后写入 id → 判定过期，清空 content

这是五级压缩中的规则级层（L2/L3），不消耗 LLM。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 会更新 file_state_map 的写工具
_WRITE_TOOLS = frozenset({"FileWriteTool", "FileEditTool", "NotepadAppendTool"})
# 可读结果可能过期的工具
_STALEABLE_READ_TOOLS = frozenset({"FileReadTool", "NotepadReadTool"})


def update_file_state_map(
    file_state_map: dict[str, str],
    *,
    tool_name: str,
    tool_result: dict[str, Any],
    message_id: str,
) -> None:
    """写工具成功后更新 file_state_map"""
    if tool_name not in _WRITE_TOOLS and tool_name != "BashTool":
        # Bash 可能改文件，但路径不确定；仅在结果含 path 时记录
        pass
    path = tool_result.get("path")
    if not path:
        return
    if tool_name in _WRITE_TOOLS or tool_name == "BashTool":
        if tool_result.get("ok") is True or tool_result.get("type") in {"create", "update"}:
            file_state_map[str(path)] = message_id


def microcompact_messages(
    messages: list[Any],
    file_state_map: dict[str, str] | None = None,
    *,
    max_tool_result_chars: int = 8_000,
) -> list[Any]:
    """清理过期 / 过大的 tool_result，保留调用轨迹

    - 过期 FileRead：替换 content 为 stale 提示
    - 超大 tool_result：截断字符串字段
    """
    file_state_map = file_state_map or {}
    # 优先使用 ToolMessage.id（与 file_state_map 写入时一致）；否则回退序号
    annotated: list[tuple[str, Any]] = []
    for idx, msg in enumerate(messages):
        existing_id = getattr(msg, "id", None)
        mid = str(existing_id) if existing_id else f"msg-{idx:05d}"
        annotated.append((mid, msg))

    # 以传入的 file_state_map 为准；仅在缺失时从带稳定 id 的写工具结果补全
    live_map = dict(file_state_map)
    if not live_map:
        for mid, msg in annotated:
            if not isinstance(msg, ToolMessage):
                continue
            parsed = _parse(msg.content)
            if isinstance(parsed, dict) and getattr(msg, "id", None):
                update_file_state_map(live_map, tool_name=str(msg.name or ""), tool_result=parsed, message_id=mid)

    compacted: list[Any] = []
    for mid, msg in annotated:
        if not isinstance(msg, ToolMessage):
            compacted.append(msg)
            continue
        name = str(msg.name or "")
        parsed = _parse(msg.content)
        if not isinstance(parsed, dict):
            compacted.append(msg)
            continue

        changed = False
        if name in _STALEABLE_READ_TOOLS:
            path = str(parsed.get("path") or "")
            last_write = live_map.get(path)
            if path and last_write and _msgid_lt(mid, last_write):
                parsed = {
                    "ok": True,
                    "stale": True,
                    "path": path,
                    "content": (
                        f"[stale] File '{path}' was modified after this read "
                        f"(write_msg={last_write}). Re-read before using this content."
                    ),
                }
                changed = True

        # 截断大字段
        for key, value in list(parsed.items()):
            if isinstance(value, str) and len(value) > max_tool_result_chars:
                parsed[key] = value[:max_tool_result_chars] + f"\n... [microcompact truncated {len(value)} chars]"
                changed = True

        if changed:
            compacted.append(
                ToolMessage(
                    content=json.dumps(parsed, ensure_ascii=False, default=str),
                    name=name,
                    tool_call_id=msg.tool_call_id,
                    # 必须保留 id，否则 add_messages 无法原地替换，会重复追加
                    id=getattr(msg, "id", None),
                )
            )
        else:
            compacted.append(msg)

    return compacted


def force_compact_messages(messages: list[Any], *, keep_last: int = 12) -> list[Any]:
    """应急压缩：保留系统消息 + 最近 keep_last 条，中间折叠为摘要占位"""
    if len(messages) <= keep_last + 2:
        return messages
    head: list[Any] = []
    tail = messages[-keep_last:]
    tail_ids = {id(m) for m in tail}
    for msg in messages:
        # 跳过已落入 tail 的消息，避免同一 SystemMessage 在 head 与 tail 重复（m5）
        if id(msg) in tail_ids:
            continue
        # 保留最前面的 system-like
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role in {"system", "SystemMessage"} or msg.__class__.__name__ == "SystemMessage":
            head.append(msg)
            if len(head) >= 2:
                break
    from langchain_core.messages import HumanMessage
    summary = HumanMessage(
        content=(
            f"[context compact] Dropped {len(messages) - len(head) - len(tail)} older messages. "
            "Continue from recent turns. Re-read files if unsure."
        )
    )
    return head + [summary] + list(tail)


def _parse(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    try:
        return json.loads(str(content))
    except (json.JSONDecodeError, TypeError):
        return content


def _msgid_lt(a: str, b: str) -> bool:
    """比较消息 id；支持 msg-00012 形式与普通字符串"""
    def _key(x: str) -> tuple[int, str]:
        if x.startswith("msg-"):
            try:
                return (0, f"{int(x[4:]):08d}")
            except ValueError:
                return (1, x)
        return (1, x)
    return _key(a) < _key(b)
