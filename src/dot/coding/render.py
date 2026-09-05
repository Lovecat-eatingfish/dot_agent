"""
dot.coding.render — 事件流 → 展示的统一渲染

事件分类逻辑收敛在这里（原先 console / one-shot CLI / TUI 各有一份 isinstance 链），
输出通过 EventSink 抽象解耦：

- ConsoleSink：交互 console，print 到 stdout
- LogSink    ：one-shot CLI，logger.info

TUI 侧的 TuiEventAdapter 是 widget 状态机（归组/流式替换），职责不同，不复用此抽象。
"""
from __future__ import annotations

import logging
from typing import Protocol

from dot.ai.events import TextDeltaEvent, ThinkingDeltaEvent
from dot.ai.types import AssistantMessage
from dot.agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    ContextCompactedEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from dot.workflow import WorkflowErrorEvent, WorkflowNodeStartEvent

logger = logging.getLogger(__name__)

TOOL_RESULT_PREVIEW_CHARS = 200
ASSISTANT_LOG_PREVIEW_CHARS = 500


class EventSink(Protocol):
    """事件展示出口（console 的 print / CLI 的 logger）"""

    def on_phase(self, node: str) -> None: ...

    def on_tool_start(self, tool_name: str, args: str) -> None: ...

    def on_tool_end(self, tool_name: str, is_error: bool, detail: str) -> None: ...

    def on_assistant_text(self, text: str) -> None: ...

    def on_workflow_error(self, error: str) -> None: ...

    def on_compaction(self, level: str, before: int, after: int, reason: str) -> None: ...


class StreamRenderer:
    """有状态的流式渲染器：累积 text delta，MessageEnd 时冲刷给 sink"""

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        self._buffer: list[str] = []

    def process(self, event) -> None:
        """分类单个事件并推送到 sink"""
        if isinstance(event, (AgentStartEvent, AgentEndEvent, TurnStartEvent, TurnEndEvent)):
            return
        if isinstance(event, WorkflowNodeStartEvent):
            self._sink.on_phase(event.node)
            return
        if isinstance(event, WorkflowErrorEvent):
            self._sink.on_workflow_error(event.error)
            return
        if isinstance(event, MessageStartEvent):
            msg = event.message
            if isinstance(msg, AssistantMessage) and msg.text:
                self._buffer.append(msg.text)
            return
        if isinstance(event, MessageUpdateEvent):
            nested = getattr(event, "provider_event", None)
            if isinstance(nested, TextDeltaEvent):
                self._buffer.append(nested.delta)
            return
        if isinstance(event, MessageEndEvent):
            msg = event.message
            if isinstance(msg, AssistantMessage):
                text = "".join(self._buffer)
                if text:
                    self._sink.on_assistant_text(text)
                self._buffer.clear()
            return
        if isinstance(event, ToolExecutionStartEvent):
            self._sink.on_tool_start(event.tool_name, str(event.args))
            return
        if isinstance(event, ToolExecutionUpdateEvent):
            return  # 跳过中间更新
        if isinstance(event, ToolExecutionEndEvent):
            detail = event.result.text[:TOOL_RESULT_PREVIEW_CHARS] if event.result else ""
            self._sink.on_tool_end(event.tool_name, event.is_error, detail)
            return
        if isinstance(event, ContextCompactedEvent):
            self._sink.on_compaction(event.level, event.before, event.after, event.reason)
            return

    def flush_partial(self) -> str:
        """中断时冲刷未完成的部分文本，返回冲刷内容"""
        partial = "".join(self._buffer)
        self._buffer.clear()
        if partial:
            self._sink.on_assistant_text(partial)
        return partial


class ConsoleSink:
    """交互 console 的展示出口（print 到 stdout）"""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logging.getLogger("console")

    def on_phase(self, node: str) -> None:
        print(f"\n--- workflow: {node} ---")

    def on_tool_start(self, tool_name: str, args: str) -> None:
        print(f"- {tool_name}")

    def on_tool_end(self, tool_name: str, is_error: bool, detail: str) -> None:
        status = "FAIL" if is_error else "OK"
        print(f"  {status}: {detail}")

    def on_assistant_text(self, text: str) -> None:
        print(text)
        self._log.info("[assistant] %s", text[:ASSISTANT_LOG_PREVIEW_CHARS])

    def on_workflow_error(self, error: str) -> None:
        print(f"workflow error: {error}")

    def on_compaction(self, level: str, before: int, after: int, reason: str) -> None:
        print(f"[context compacted {level}] {before} -> {after} messages ({reason})")


class LogSink:
    """one-shot CLI 的展示出口（logger.info）"""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def on_phase(self, node: str) -> None:
        self._log.info("─── phase: %s ───", node)

    def on_tool_start(self, tool_name: str, args: str) -> None:
        self._log.info("[tool] %s %s", tool_name, args[:150])

    def on_tool_end(self, tool_name: str, is_error: bool, detail: str) -> None:
        status = "FAIL" if is_error else "OK"
        self._log.info("[tool] %s [%s] %s", tool_name, status, detail[:150])

    def on_assistant_text(self, text: str) -> None:
        if text.strip():
            for line in text.rstrip().splitlines():
                self._log.info("[ai] %s", line)

    def on_workflow_error(self, error: str) -> None:
        pass  # one-shot CLI 原实现不展示 WorkflowErrorEvent

    def on_compaction(self, level: str, before: int, after: int, reason: str) -> None:
        self._log.info("[compact] %s: %d -> %d messages", level, before, after)
