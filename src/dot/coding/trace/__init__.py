"""
dot.coding.trace — 事件驱动链路追踪

TraceCollector 订阅 AgentEvent 流，自动生成 span 树。
不使用手动埋点——agent loop 自动发射事件。
"""
from __future__ import annotations

from .collector import TraceCollector, NoopTraceCollector, Span
from .exporter import LocalFileTraceExporter

__all__ = [
    "TraceCollector",
    "NoopTraceCollector",
    "Span",
    "LocalFileTraceExporter",
]
