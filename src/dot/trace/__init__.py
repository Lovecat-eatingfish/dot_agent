"""
dot.trace — 链路追踪（doc/fix-链路追踪.md）

  - exporter: LocalFileTraceExporter（JSONL 异步写入 + 按天目录 + 7 天清理）
  - tracer:  Tracer 抽象 / Span（OTel 语义字段）/ contextvar 上下文贯穿

埋点分层：session(turn) → graph_node → llm / mcp / session(persist_turn)
文件位置：<workspace>/.dot/traces/<YYYY-MM-DD>/trace_{session_id}.jsonl
"""
from __future__ import annotations

from .exporter import LocalFileTraceExporter
from .tracer import (
    NoopTracer,
    Span,
    Tracer,
    activate_span,
    deactivate_span,
    get_tracer,
    init_tracer,
    reset_session_context,
    reset_tracer,
    set_session_context,
)

__all__ = [
    "LocalFileTraceExporter",
    "Tracer",
    "NoopTracer",
    "Span",
    "get_tracer",
    "init_tracer",
    "reset_tracer",
    "set_session_context",
    "reset_session_context",
    "activate_span",
    "deactivate_span",
]
