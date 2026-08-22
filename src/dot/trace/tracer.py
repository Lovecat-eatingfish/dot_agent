"""
Tracer — 链路追踪抽象层（doc/fix-链路追踪.md）

  - Span 字段对齐 OpenTelemetry 语义：trace_id / span_id / parent_span_id /
    timestamp / duration_ms / service / name / status / tags /
    input_summary / output_summary / error_stack
  - 埋点与存储解耦：Tracer 只依赖 export(span: dict)；
    本地 JSONL 只是其中一种 Exporter，换 OTel/Langfuse 不改业务埋点
  - 上下文贯穿：contextvar 维护 current span + session_id，
    子 span 自动挂 parent、自动携带 session_id（MCP/LLM/节点全链路同 trace_id）
  - 摘要截断：input/output 只保留前 SUMMARY_LIMIT 字符，防文件膨胀

用法：
    from dot.trace import get_tracer

    with get_tracer().start_span("graph_node", "plan_node") as span:
        span.set_tag("model", "gpt-4o")
        ...                                  # 异常时自动记 error_stack 并 re-raise
        span.set_output_summary("plan ok")
"""
from __future__ import annotations

import time
import traceback
import uuid
from abc import ABC, abstractmethod
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.log import get_logger
from .exporter import LocalFileTraceExporter

logger = get_logger(__name__)

# 摘要截断长度（避坑 #1：不存全量 prompt/返回值）
SUMMARY_LIMIT = 200

# service 分类（对齐设计文档）
SERVICE_AGENT_HOST = "agent_host"
SERVICE_GRAPH_NODE = "graph_node"
SERVICE_MCP = "mcp"
SERVICE_LLM = "llm"
SERVICE_SESSION = "session"

# 当前 span（子 span 自动挂 parent）
_current_span: ContextVar[Optional["Span"]] = ContextVar("dot_current_span", default=None)
# 当前 session_id（所有 span 自动携带，exporter 按它分文件）
_current_session_id: ContextVar[str] = ContextVar("dot_session_id", default="default")


def set_session_context(session_id: str) -> None:
    """设置当前 session 上下文（每轮 turn 开始时调用）"""
    _current_session_id.set(session_id or "default")


def activate_span(span: "Span"):
    """激活 span 上下文并开始计时，返回 token（generator 场景用）

    配合 deactivate_span(token) + span.finish() 使用，
    与 with span 等价但允许手动控制结束时机。
    """
    span._begin()
    return _current_span.set(span)


def deactivate_span(token) -> None:
    """退出 span 上下文（不 finish）"""
    _current_span.reset(token)


def _new_id(n: int = 16) -> str:
    return uuid.uuid4().hex[:n]


def _now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _truncate(text: Any, limit: int = SUMMARY_LIMIT) -> str:
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


class Span:
    """单个追踪单元；正常/异常退出都会导出（避坑 #4）"""

    def __init__(
        self,
        tracer: "Tracer",
        service: str,
        name: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str = "",
        tags: Optional[dict[str, Any]] = None,
        input_summary: str = "",
    ) -> None:
        self._tracer = tracer
        self.service = service
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.tags: dict[str, Any] = dict(tags or {})
        self.input_summary = _truncate(input_summary)
        self.output_summary = ""
        self.status = "ok"
        self.error_stack = ""
        self._start = 0.0
        self._timestamp = ""
        self._finished = False

    # ----------------------------------------------------------
    # 数据写入
    # ----------------------------------------------------------

    def set_tag(self, key: str, value: Any) -> "Span":
        self.tags[key] = value
        return self

    def set_tags(self, tags: dict[str, Any]) -> "Span":
        self.tags.update(tags)
        return self

    def set_input_summary(self, text: Any) -> "Span":
        self.input_summary = _truncate(text)
        return self

    def set_output_summary(self, text: Any) -> "Span":
        self.output_summary = _truncate(text)
        return self

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------

    def _begin(self) -> "Span":
        self._start = time.perf_counter()
        self._timestamp = _now_iso_ms()
        return self

    def finish(self, exc: Optional[BaseException] = None) -> None:
        """结束 span 并导出（幂等）"""
        if self._finished:
            return
        self._finished = True
        duration_ms = round((time.perf_counter() - self._start) * 1000, 2)
        if exc is not None:
            self.status = "error"
            self.error_stack = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[:4000]
        record = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "timestamp": self._timestamp,
            "duration_ms": duration_ms,
            "service": self.service,
            "name": self.name,
            "status": self.status,
            "tags": self.tags,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "error_stack": self.error_stack,
        }
        self._tracer.export(record)

    # ----------------------------------------------------------
    # 上下文管理器
    # ----------------------------------------------------------

    def __enter__(self) -> "Span":
        self._token = _current_span.set(self)
        return self._begin()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            self.finish(exc_val)
        finally:
            _current_span.reset(self._token)
        return False  # 不吞异常


class Tracer(ABC):
    """追踪器抽象接口（埋点与存储解耦）"""

    @abstractmethod
    def start_span(
        self,
        service: str,
        name: str,
        *,
        tags: Optional[dict[str, Any]] = None,
        input_summary: str = "",
        trace_id: Optional[str] = None,
    ) -> Span:
        """创建子 span：自动继承 current span 的 trace/parent 与 session_id"""

    @abstractmethod
    def export(self, record: dict[str, Any]) -> None:
        """导出一条 span 记录"""


class LocalTracer(Tracer):
    """本地实现：JSONL Exporter"""

    def __init__(self, exporter: LocalFileTraceExporter) -> None:
        self._exporter = exporter

    def start_span(
        self,
        service: str,
        name: str,
        *,
        tags: Optional[dict[str, Any]] = None,
        input_summary: str = "",
        trace_id: Optional[str] = None,
    ) -> Span:
        parent = _current_span.get()
        merged_tags = {"session_id": _current_session_id.get()}
        if tags:
            merged_tags.update(tags)
        return Span(
            tracer=self,
            service=service,
            name=name,
            # 构建树状结构，每个 span 都有 trace_id 与 parent_span_id 关联
            trace_id=trace_id or (parent.trace_id if parent else _new_id(32)),
            span_id=_new_id(16),
            parent_span_id=parent.span_id if parent else "",
            tags=merged_tags,
            input_summary=input_summary,
        )

    def export(self, record: dict[str, Any]) -> None:
        try:
            self._exporter.export(record)
        except Exception as exc:
            logger.debug("[trace] export failed: %s", exc)


class NoopTracer(Tracer):
    """关闭追踪时的空实现（零开销）"""

    def start_span(self, service, name, *, tags=None, input_summary="", trace_id=None) -> Span:
        return Span(tracer=self, service=service, name=name,
                    trace_id=trace_id or _new_id(32), span_id=_new_id(16))

    def export(self, record: dict[str, Any]) -> None:
        pass


# ============================================================
# 全局单例
# ============================================================

_tracer: Optional[Tracer] = None
_tracer_lock = __import__("threading").Lock()


def init_tracer(workspace=None, *, enabled: bool | None = None) -> Tracer:
    """初始化全局 tracer（AgentHost 启动时调用一次）

    Args:
        workspace: 工作区（traces 落在 <workspace>/.dot/traces/）
        enabled: None 时读环境变量 DOT_TRACE_ENABLED（默认开启）
    """
    global _tracer
    import os
    from pathlib import Path

    if enabled is None:
        enabled = os.environ.get("DOT_TRACE_ENABLED", "1") not in ("0", "false", "off")

    with _tracer_lock:
        if not enabled:
            _tracer = NoopTracer()
            logger.info("[trace] disabled (DOT_TRACE_ENABLED)")
            return _tracer
        ws = Path(workspace) if workspace is not None else Path.cwd()
        exporter = LocalFileTraceExporter(ws / ".dot" / "traces")
        _tracer = LocalTracer(exporter)
        return _tracer


def get_tracer() -> Tracer:
    """获取全局 tracer（未初始化时懒初始化到 cwd）"""
    global _tracer
    if _tracer is None:
        init_tracer()
    return _tracer


def reset_tracer() -> None:
    """重置全局 tracer（测试用）"""
    global _tracer
    with _tracer_lock:
        _tracer = None
