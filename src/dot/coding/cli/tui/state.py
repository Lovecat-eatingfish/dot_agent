"""
dot.coding.cli.tui.state — TUI 显示状态

参考 Tau 的 TuiState 设计：
- ChatItem 列表作为显示单元
- Tool 批处理与分组
- Assistant buffer 流式累积
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from dot.ai.types import AssistantMessage, ToolCall, UserMessage, TextContent

ChatItemRole = Literal["user", "assistant", "tool", "thinking", "error", "status"]

TOOL_RESULT_PREVIEW_LINES = 8
TOOL_RESULT_PREVIEW_CHARS = 2000


@dataclass(slots=True)
class ChatItem:
    """TUI 中的一条显示项"""
    role: ChatItemRole
    text: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict | None = None
    tool_result_text: str | None = None
    update_text: str | None = None
    started_at: float | None = None
    tool_batch_id: int | None = None
    tool_batch_items: list[ChatItem] | None = None


@dataclass(slots=True)
class TuiState:
    """TUI 可变显示状态"""
    items: list[ChatItem] = field(default_factory=list)
    assistant_buffer: str = ""
    running: bool = False
    error: str | None = None
    show_tool_results: bool = False
    show_thinking: bool = False
    queued_steering: tuple[str, ...] = ()
    queued_follow_up: tuple[str, ...] = ()
    _tool_items_by_call_id: dict[str, ChatItem] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _next_tool_batch_id: int = field(default=0, init=False, repr=False, compare=False)

    def add_item(self, role: ChatItemRole, text: str, **kwargs) -> None:
        item = ChatItem(role=role, text=text, **kwargs)
        self.items.append(item)
        if item.tool_call_id is not None and role == "tool":
            self._tool_items_by_call_id[item.tool_call_id] = item

    def add_user_message(self, content: str) -> None:
        self.add_item("user", content)

    def add_assistant_message(self, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextContent) and block.text:
                self.add_item("assistant", block.text)

    def add_assistant_error(self, message: AssistantMessage) -> None:
        self.add_assistant_message(message)
        text = message.error_message or "Error"
        self.error = text
        self.add_item("error", f"Error: {text}")

    def add_thinking_delta(self, delta: str) -> None:
        """流式追加 thinking 文本（合并进最后一条 thinking 项）"""
        if self.items and self.items[-1].role == "thinking":
            self.items[-1].text += delta
        else:
            self.add_item("thinking", delta)

    def add_tool_call(self, tool_call: ToolCall, *, batch_id: int | None = None) -> None:
        invocation = format_tool_call_block(tool_call)
        self.add_item(
            "tool",
            invocation,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            tool_arguments=tool_call.arguments,
            started_at=time.monotonic(),
            tool_batch_id=batch_id,
        )

    def find_tool_item(self, tool_call_id: str) -> ChatItem | None:
        return self._tool_items_by_call_id.get(tool_call_id)

    def record_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result_text: str,
        is_error: bool,
    ) -> None:
        formatted = format_tool_result_block(name=tool_name, ok=not is_error, content=result_text)
        item = self.find_tool_item(tool_call_id)
        if item is not None:
            item.tool_result_text = formatted
            item.update_text = None
            item.started_at = None
        else:
            self.add_item("tool", formatted, tool_call_id=tool_call_id, tool_name=tool_name)

    def record_tool_update(self, tool_call_id: str, message: str) -> None:
        item = self.find_tool_item(tool_call_id)
        if item is not None and item.tool_result_text is None:
            item.update_text = message

    def clear(self) -> None:
        self.items.clear()
        self._tool_items_by_call_id.clear()
        self.assistant_buffer = ""
        self.error = None

    def update_queue(
        self,
        *,
        steering: tuple[str, ...] = (),
        follow_up: tuple[str, ...] = (),
    ) -> None:
        self.queued_steering = steering
        self.queued_follow_up = follow_up

    def new_tool_batch_id(self) -> int:
        self._next_tool_batch_id += 1
        return self._next_tool_batch_id


# ============================================================
# 格式化工具函数
# ============================================================

def format_tool_call_block(tool_call: ToolCall) -> str:
    args = tool_call.arguments
    if tool_call.name in ("read_file", "read"):
        path = args.get("file_path") or args.get("path", "")
        return f"→ read {path}"
    if tool_call.name in ("write_file", "write"):
        path = args.get("file_path") or args.get("path", "")
        return f"→ write {path}"
    if tool_call.name in ("edit_file", "edit"):
        path = args.get("file_path") or args.get("path", "")
        return f"→ edit {path}"
    if tool_call.name == "bash":
        cmd = str(args.get("command", ""))[:120]
        return f"$ {cmd}"
    if tool_call.name == "glob_search":
        return f"→ glob {args.get('pattern', '')}"
    if tool_call.name == "grep":
        return f"→ grep {args.get('pattern', '')}"
    return f"→ {tool_call.name}"


def format_tool_result_block(*, name: str, ok: bool, content: str) -> str:
    status = "OK" if ok else "FAIL"
    lines = [f"{status} {name}"]
    if content:
        preview = _preview_text(content, max_lines=TOOL_RESULT_PREVIEW_LINES)
        lines.append(preview)
    return "\n".join(lines)


def _preview_text(text: str, *, max_lines: int) -> str:
    lines = text.splitlines()
    if not lines:
        return text[:TOOL_RESULT_PREVIEW_CHARS]
    preview_lines = lines[:max_lines]
    preview = "\n".join(preview_lines)
    hidden = max(0, len(lines) - len(preview_lines))
    if len(preview) > TOOL_RESULT_PREVIEW_CHARS:
        preview = preview[:TOOL_RESULT_PREVIEW_CHARS].rstrip()
    if hidden:
        preview += f"\n\n[{hidden} more lines hidden]"
    return preview
