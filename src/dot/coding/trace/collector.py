"""
dot.coding.trace.collector — TraceCollector 事件驱动链路追踪

订阅 AgentEvent 流，自动生成 span 树。
不使用手动埋点——agent loop 自动发射事件。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exporter import LocalFileTraceExporter

from dot.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 200


def _truncate(text: Any, limit: int = SUMMARY_LIMIT) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit - 3] + "..."


@dataclass
class Span:
    """追踪单元"""
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    service: str = ""
    name: str = ""
    timestamp: str = ""
    duration_ms: float = 0
    status: str = "ok"
    tags: dict[str, Any] = field(default_factory=dict)
    input_summary: str = ""
    output_summary: str = ""
    error_stack: str = ""
    _start: float = 0
    _finished: bool = False

    def begin(self) -> None:
        self._start = time.perf_counter()
        self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    def finish(self, status: str = "ok", output: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        self.duration_ms = round((time.perf_counter() - self._start) * 1000, 2)
        self.status = status
        self.output_summary = _truncate(output)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "service": self.service,
            "name": self.name,
            "status": self.status,
            "tags": self.tags,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "error_stack": self.error_stack,
        }


class TraceCollector:
    """事件驱动的链路追踪收集器

    订阅 AgentEvent 流，自动生成 span 树。
    通过 LocalFileTraceExporter 落盘：
    <output_dir>/<YYYY-MM-DD>/trace_{session_id}.jsonl（按天 + 会话分文件，追加写入）。
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        *,
        session_id: str = "",
        exporter: "LocalFileTraceExporter | None" = None,
    ) -> None:
        self._output_dir = output_dir
        self._session_id = session_id
        self._exporter = exporter or (LocalFileTraceExporter(output_dir) if output_dir else None)
        self._trace_id: str = ""
        self._span_stack: list[Span] = []
        self._spans: list[Span] = []

    @property
    def output_dir(self) -> Path | None:
        """当前落盘目录（未启用时为 None）"""
        return self._exporter.dir if self._exporter else None

    def on_event(self, event: AgentEvent) -> None:
        """处理 Agent 事件，自动生成 span"""
        if isinstance(event, AgentStartEvent):
            self._trace_id = uuid.uuid4().hex[:32]
            span = self._push_span("agent", "agent")
            span.begin()

        elif isinstance(event, TurnStartEvent):
            span = self._push_span("turn", "turn")
            span.begin()

        elif isinstance(event, ToolExecutionStartEvent):
            span = self._push_span("tool", f"tool:{event.tool_name}")
            span.tags["tool_name"] = event.tool_name
            span.tags["tool_call_id"] = event.tool_call_id
            span.input_summary = _truncate(str(event.args))
            span.begin()

        elif isinstance(event, ToolExecutionEndEvent):
            span = self._pop_span()
            if span:
                status = "error" if event.is_error else "ok"
                span.finish(status=status, output=event.result.text)

        elif isinstance(event, TurnEndEvent):
            span = self._pop_span()
            if span:
                span.finish(output=event.message.text if hasattr(event.message, "text") else "")

        elif isinstance(event, AgentEndEvent):
            span = self._pop_span()
            if span:
                span.finish()
            self._flush()

    def flush(self) -> None:
        """兜底落盘：未结束的 span（如中断）标记为 interrupted 后写出，并等待写完"""
        for span in self._span_stack:
            span.finish(status="interrupted")
        self._span_stack.clear()
        self._flush()
        if self._exporter is not None and hasattr(self._exporter, "wait_flushed"):
            self._exporter.wait_flushed()

    def _push_span(self, service: str, name: str) -> Span:
        # 如果有父 span，设置为当前 span 的 id
        parent_id = self._span_stack[-1].span_id if self._span_stack else ""
        span = Span(
            trace_id=self._trace_id,
            span_id=uuid.uuid4().hex[:16],
            # 链接到父节点 span
            parent_span_id=parent_id,
            service=service,
            name=name,
        )
        if self._session_id:
            span.tags["session_id"] = self._session_id
        # 保存 span 到栈和列表
        self._span_stack.append(span)
        self._spans.append(span)
        return span

    def _pop_span(self) -> Span | None:
        return self._span_stack.pop() if self._span_stack else None

    def _flush(self) -> None:
        """将 spans 通过 exporter 写出"""
        if not self._exporter or not self._spans:
            return
        for span in self._spans:
            self._exporter.export(span.to_dict())
        self._spans.clear()


class NoopTraceCollector:
    """空实现，关闭追踪时零开销"""

    def on_event(self, event: AgentEvent) -> None:
        pass

    def flush(self) -> None:
        pass
