"""
dot.coding.cli.tui.widgets — TUI 自定义 widgets

TranscriptView：滚动消息容器（follow-to-bottom）。
StreamingMarkdown：基于 Textual MarkdownStream 的流式 markdown。
ToolLine：工具调用行（默认一行 invocation，可展开结果）。
StatusBar：底部状态栏（模式/工作区/会话/上下文水位/运行状态）。
"""
from __future__ import annotations

import time

from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from .state import ChatItem, TuiState, format_tool_call_block


class TranscriptView(VerticalScroll):
    """滚动消息容器 — 用户上滚即停止跟随，回到底部恢复跟随"""

    DEFAULT_CSS = """
    TranscriptView { padding: 0 1; }
    TranscriptView .msg-user { color: $accent; margin: 1 0 0 0; }
    TranscriptView .msg-assistant { margin: 0 0 1 0; }
    TranscriptView .msg-thinking { color: $text-muted; text-style: italic; margin: 0 0 1 2; }
    TranscriptView .tool-line { color: $warning; margin: 0 0 0 2; }
    TranscriptView .tool-result { color: $text-muted; margin: 0 0 1 4; }
    TranscriptView .msg-error { color: $error; text-style: bold; margin: 1 0; }
    TranscriptView .msg-status { color: $text-muted; margin: 0 0 1 0; }
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._following = True
        self._streaming: Markdown | None = None
        self._tool_widgets: dict[str, ToolLine] = {}

    def _user_scrolled_away(self) -> bool:
        return self.scroll_offset.y + self.size.height < self.max_scroll_y - 2

    async def append(self, widget) -> None:
        """挂载一个子 widget；若用户未上滚则自动跟随到底部"""
        follow = self._following and not self._user_scrolled_away()
        await self.mount(widget)
        if follow:
            self.scroll_end(animate=False)

    async def start_streaming(self) -> Markdown:
        """开始一个新的流式 assistant 消息"""
        await self.finish_streaming()
        md = Markdown("", classes="msg-assistant")
        self._streaming = md
        await self.append(md)
        return md

    async def stream_delta(self, delta: str) -> None:
        """向当前流式消息写入增量（MarkdownStream 增量渲染，不重解析全量）"""
        if self._streaming is None:
            await self.start_streaming()
        md = self._streaming
        if md is None or not delta:
            return
        stream = md.get_stream(md)
        await stream.write(delta)
        if self._following and not self._user_scrolled_away():
            self.scroll_end(animate=False)

    async def finish_streaming(self, final_text: str | None = None) -> None:
        """结束流式：用最终文本整体校正渲染"""
        md = self._streaming
        self._streaming = None
        if md is None:
            return
        if final_text is not None and final_text.strip():
            md.update(final_text)
        elif not (final_text or "").strip():
            # 空消息：移除占位 widget
            await md.remove()

    # ============================================================
    # 各类消息行
    # ============================================================

    async def add_user(self, text: str) -> None:
        await self.finish_streaming()
        await self.append(Static(f"❯ {text}", classes="msg-user"))

    async def add_thinking(self, text: str) -> None:
        preview = text if len(text) <= 300 else text[:300] + "…"
        await self.append(Static(f"· thinking: {preview}", classes="msg-thinking"))

    async def add_error(self, text: str) -> None:
        await self.append(Static(f"✗ {text}", classes="msg-error"))

    async def add_status(self, text: str) -> None:
        await self.append(Static(text, classes="msg-status"))

    async def add_tool(self, item: ChatItem) -> None:
        """工具调用行：invocation 一行 + 折叠的结果区"""
        await self.finish_streaming()
        widget = ToolLine(item)
        if item.tool_call_id:
            self._tool_widgets[item.tool_call_id] = widget
        await self.append(widget)

    def update_tool(self, item: ChatItem) -> None:
        """工具结果/进度到达：原地更新对应 ToolLine（不重挂载）"""
        widget = self._tool_widgets.get(item.tool_call_id or "")
        if widget is not None:
            widget.refresh_from_item(item)


class ToolLine(Static):
    """工具调用行：默认折叠只显示 invocation；show_tool_results 展开结果预览"""

    DEFAULT_CSS = """
    ToolLine { margin: 0 0 0 2; }
    ToolLine .tool-invocation { color: $warning; }
    ToolLine .tool-result { color: $text-muted; display: none; margin: 0 0 0 2; }
    ToolLine.expanded .tool-result { display: block; }
    """

    def __init__(self, item: ChatItem) -> None:
        super().__init__()
        self.item = item

    def on_mount(self) -> None:
        self._rerender()

    def refresh_from_item(self, item: ChatItem) -> None:
        self.item = item
        self._rerender()

    def set_expanded(self, expanded: bool) -> None:
        self.set_class(expanded, "expanded")
        self._rerender()

    def _rerender(self) -> None:
        item = self.item
        invocation = item.text or format_tool_call_block_from_item(item)
        lines = [f"→ {invocation}"]
        if item.tool_result_text is not None:
            lines.append(item.tool_result_text)
        elif item.update_text:
            lines.append(f"  … {item.update_text[:120]}")
        self.update("\n".join(lines))


def format_tool_call_block_from_item(item: ChatItem) -> str:
    """从 ChatItem 还原 invocation 文本（兜底）"""
    from dot.ai.types import ToolCall

    if item.tool_arguments is not None and item.tool_name:
        return format_tool_call_block(
            ToolCall(id=item.tool_call_id or "", name=item.tool_name, arguments=item.tool_arguments),
        )
    return item.tool_name or ""


class StatusBar(Static):
    """底部状态栏：mode | workspace | session | 上下文水位 | 运行状态"""

    DEFAULT_CSS = """
    StatusBar { dock: bottom; height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    """

    def render_state(self, state: TuiState, *, mode_label: str, workspace, session_id: str) -> None:
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10] if state.running else "✓"
        status = f"{spinner} running" if state.running else "idle"
        self.update(
            f" {mode_label} │ {workspace} │ session {session_id} │ {status} │ /help for commands"
        )


class QueueStatus(Static):
    """Compact preview of messages waiting behind the active run."""

    DEFAULT_CSS = """
    QueueStatus { height: auto; max-height: 4; display: none; color: $text-muted;
                  padding: 0 1; background: $panel; }
    QueueStatus.visible { display: block; }
    """

    def render_state(self, state: TuiState) -> None:
        lines: list[str] = []
        if state.queued_steering:
            lines.append(self._line("steer", state.queued_steering))
        if state.queued_follow_up:
            lines.append(self._line("follow-up", state.queued_follow_up))
        self.update("\n".join(lines))
        self.set_class(bool(lines), "visible")

    @staticmethod
    def _line(label: str, messages: tuple[str, ...]) -> str:
        preview = messages[-1].replace("\n", " ").strip()
        if len(preview) > 100:
            preview = preview[:97] + "..."
        suffix = f": {preview}" if preview else ""
        return f"{label} {len(messages)}{suffix}"
