"""
dot.coding.trace — 事件驱动链路追踪

TraceCollector 订阅 AgentEvent 流，自动生成 span 树。
不使用手动埋点——agent loop 自动发射事件。

开关：环境变量 DOT_TRACE_ENABLED=0 时降级为 NoopTraceCollector（零开销空实现）。
"""
from __future__ import annotations

import os
from pathlib import Path

from .collector import TraceCollector, NoopTraceCollector, Span
from .exporter import LocalFileTraceExporter

__all__ = [
    "TraceCollector",
    "NoopTraceCollector",
    "Span",
    "LocalFileTraceExporter",
    "make_trace_collector",
    "trace_enabled",
]


def trace_enabled() -> bool:
    """是否启用追踪（DOT_TRACE_ENABLED=0 关闭，默认开启）"""
    return os.environ.get("DOT_TRACE_ENABLED", "1") != "0"


def make_trace_collector(workspace: Path, session_id: str) -> TraceCollector | NoopTraceCollector:
    """构造追踪收集器：写入 <workspace>/.dot/traces/，关闭时返回 Noop 实现"""
    if not trace_enabled():
        return NoopTraceCollector()
    return TraceCollector(
        output_dir=workspace / ".dot" / "traces",
        session_id=session_id,
    )
