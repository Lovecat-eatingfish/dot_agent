"""
Agent 全链路追踪模块

轻量自研实现，不引入完整 OpenTelemetry SDK。
包含 Trace/Span 数据模型、StorageProvider 存储抽象、TraceManager 对外 API。

集成点：
- SessionManager 的每个 Session 持有 trace_manager
- stream_session_events 每次调用对应一次 Trace
- 节点内部、工具执行层通过 trace_manager 埋点
"""
from __future__ import annotations

import json
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ============================================================
# Enums
# ============================================================

class TraceStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    CANCEL = "cancel"


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


class SpanType(str, Enum):
    LLM_INVOKE = "llm_invoke"
    META_TOOL = "meta_tool"
    MCP_CALL = "mcp_call"
    SKILL_LOAD = "skill_load"
    AGENT_TURN = "agent_turn"


# ============================================================
# Data Models
# ============================================================

@dataclass
class Span:
    """链路追踪 Span"""
    span_id: str
    trace_id: str
    span_type: str
    name: str
    start_time: int  # ms timestamp
    end_time: int = 0
    duration_ms: int = 0
    parent_span_id: Optional[str] = None
    status: str = SpanStatus.OK.value
    error: Optional[dict[str, Any]] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Trace:
    """链路追踪 Trace（一次用户提问 = 一条 trace）"""
    trace_id: str
    session_id: str
    user_query: str
    start_time: int
    end_time: int = 0
    total_duration_ms: int = 0
    status: str = TraceStatus.SUCCESS.value
    error_msg: Optional[str] = None
    root_span_id: Optional[str] = None


@dataclass
class TraceTree:
    """Trace 树结构，用于可视化渲染"""
    trace: Trace
    spans: list[Span]
    # 构建父子关系的 span 树
    span_map: dict[str, Span] = field(default_factory=dict)
    children_map: dict[str, list[Span]] = field(default_factory=dict)


# ============================================================
# StorageProvider 抽象接口
# ============================================================

class StorageProvider(ABC):
    """存储抽象基类

    后期对接 MySQL / Elasticsearch 时，实现此接口即可，
    业务埋点代码（TraceManager）无需改动。
    """

    @abstractmethod
    async def save_trace(self, trace: Trace) -> None:
        """保存 trace"""
        ...

    @abstractmethod
    async def save_span(self, span: Span) -> None:
        """保存 span"""
        ...

    @abstractmethod
    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        """获取单个 trace"""
        ...

    @abstractmethod
    async def list_spans_by_trace_id(self, trace_id: str) -> list[Span]:
        """获取 trace 下所有 span"""
        ...

    @abstractmethod
    async def list_traces_by_session_id(self, session_id: str) -> list[Trace]:
        """按 session_id 查询所有 trace"""
        ...

    @abstractmethod
    async def get_trace_tree(self, trace_id: str) -> Optional[TraceTree]:
        """获取 trace 完整树结构"""
        ...


# ============================================================
# FileStorageProvider（默认本地 JSON 实现）
# ============================================================

class FileStorageProvider(StorageProvider):
    """本地 JSON 文件存储实现

    目录结构：
    .agent_traces/
    └─{session_id}/
      ├─traces.jsonl          # trace 日志（每行一条 JSON）
      └─spans/
        └─{trace_id}.jsonl    # 每个 trace 的 span 日志
    """

    def __init__(self, root: Path = Path(".agent_traces")) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _traces_path(self, session_id: str) -> Path:
        return self._root / session_id / "traces.jsonl"

    def _spans_dir(self) -> Path:
        return self._root / "spans"

    def _spans_path(self, trace_id: str) -> Path:
        return self._spans_dir() / f"{trace_id}.jsonl"

    async def save_trace(self, trace: Trace) -> None:
        path = self._traces_path(trace.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_trace_to_dict(trace), ensure_ascii=False, default=str) + "\n")

    async def save_span(self, span: Span) -> None:
        span_path = self._spans_path(span.trace_id)
        span_path.parent.mkdir(parents=True, exist_ok=True)
        with open(span_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_span_to_dict(span), ensure_ascii=False, default=str) + "\n")

    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        if not self._root.exists():
            return None
        for session_dir in self._root.iterdir():
            if not session_dir.is_dir():
                continue
            path = session_dir / "traces.jsonl"
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        if data.get("trace_id") == trace_id:
                            return _dict_to_trace(data)
                    except (json.JSONDecodeError, KeyError):
                        continue
        return None

    async def list_spans_by_trace_id(self, trace_id: str) -> list[Span]:
        spans_dir = self._spans_dir()
        if not spans_dir.exists():
            return []
        path = spans_dir / f"{trace_id}.jsonl"
        if not path.exists():
            return []
        result = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    result.append(_dict_to_span(data))
                except (json.JSONDecodeError, KeyError):
                    continue
        return result

    async def list_traces_by_session_id(self, session_id: str) -> list[Trace]:
        path = self._traces_path(session_id)
        if not path.exists():
            return []
        result = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    result.append(_dict_to_trace(data))
                except (json.JSONDecodeError, KeyError):
                    continue
        return result

    async def get_trace_tree(self, trace_id: str) -> Optional[TraceTree]:
        trace = await self.get_trace(trace_id)
        if trace is None:
            return None
        spans = await self.list_spans_by_trace_id(trace_id)
        span_map = {s.span_id: s for s in spans}
        children_map: dict[str, list[Span]] = {}
        root_span = None
        for span in spans:
            if span.parent_span_id is None:
                root_span = span
            else:
                children_map.setdefault(span.parent_span_id, []).append(span)
        return TraceTree(
            trace=trace,
            spans=spans,
            span_map=span_map,
            children_map=children_map,
        )


# ============================================================
# TraceManager（对外 API）
# ============================================================

class TraceManager:
    """链路追踪管理器

    对外 API：
    - set_storage_provider(provider)
    - start_trace(sessionId, userQuery) → Trace
    - start_span(parentSpanId, traceId, spanType, name) → Span
    - end_span(spanId, status, error?)
    - add_span_event(spanId, eventName, payload?)
    - get_trace_tree(traceId) → TraceTree
    """

    def __init__(self, storage: Optional[StorageProvider] = None) -> None:
        self._storage = storage or FileStorageProvider()
        # trace_id → Trace（运行中）
        self._active_traces: dict[str, Trace] = {}
        # span_id → Span（运行中，用于 end_span 时查找）
        self._active_spans: dict[str, Span] = {}

    def set_storage_provider(self, provider: StorageProvider) -> None:
        """切换存储实现"""
        self._storage = provider

    def start_trace(self, session_id: str, user_query: str) -> Trace:
        """开启一次 Agent 轮次，返回 trace 对象"""
        trace_id = _uuid()
        trace = Trace(
            trace_id=trace_id,
            session_id=session_id,
            user_query=user_query,
            start_time=_now_ms(),
        )
        self._active_traces[trace_id] = trace

        # 创建 root span: agent_turn
        root_span = self.start_span(
            parent_span_id=None,
            trace_id=trace_id,
            span_type=SpanType.AGENT_TURN.value,
            name="agent_turn",
        )
        trace.root_span_id = root_span.span_id

        # 异步持久化 trace
        self._safe_save(trace)

        return trace

    def start_span(
        self,
        parent_span_id: Optional[str],
        trace_id: str,
        span_type: str,
        name: str,
    ) -> Span:
        """创建子 span"""
        span_id = _uuid()
        span = Span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            span_type=span_type,
            name=name,
            start_time=_now_ms(),
        )
        self._active_spans[span_id] = span
        return span

    def end_span(
        self,
        span_id: str,
        status: str = SpanStatus.OK.value,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        """结束 span，自动计算 duration，调用存储保存"""
        span = self._active_spans.pop(span_id, None)
        if span is None:
            return
        span.end_time = _now_ms()
        span.duration_ms = span.end_time - span.start_time
        span.status = status
        if error:
            span.error = error
        self._safe_save(span)

    def add_span_event(
        self,
        span_id: str,
        event_name: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """添加 span 内部事件"""
        span = self._active_spans.get(span_id)
        if span is None:
            return
        span.events.append({
            "timestamp": _now_ms(),
            "name": event_name,
            "payload": _truncate_value(payload),
        })

    def end_trace(self, trace_id: str, status: str = TraceStatus.SUCCESS.value, error_msg: Optional[str] = None) -> None:
        """结束 trace"""
        trace = self._active_traces.pop(trace_id, None)
        if trace is None:
            return
        trace.end_time = _now_ms()
        trace.total_duration_ms = trace.end_time - trace.start_time
        trace.status = status
        if error_msg:
            trace.error_msg = error_msg
        self._safe_save(trace)

    def get_active_span(self, span_id: str) -> Optional[Span]:
        """获取运行中的 span"""
        return self._active_spans.get(span_id)

    def get_trace_tree(self, trace_id: str) -> Optional[TraceTree]:
        """获取 trace 完整树结构（同步封装异步方法）"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在异步上下文中，返回协程让调用方 await
                return self._storage.get_trace_tree(trace_id)
            return loop.run_until_complete(self._storage.get_trace_tree(trace_id))
        except RuntimeError:
            return None

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _safe_save(self, obj: Any) -> None:
        """异步持久化，不阻塞主流程"""
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return
            coro = self._storage.save_trace(obj) if isinstance(obj, Trace) else self._storage.save_span(obj)
            loop.run_until_complete(coro)
        except (RuntimeError, Exception):
            pass


# ============================================================
# TraceContext（线程本地上下文）
# ============================================================

class TraceContext:
    """当前执行上下文的 trace/span 上下文

    使用 threading.local 存储当前线程的 trace_id 和当前 span_id。
    埋点时通过 TraceContext 获取当前活跃的 trace/span，无需层层传递。
    """

    _local: Any = None

    @classmethod
    def _get_local(cls) -> Any:
        if cls._local is None:
            import threading
            cls._local = threading.local()
        return cls._local

    @classmethod
    def set_current_trace(cls, trace_id: str, current_span_id: Optional[str] = None) -> None:
        local = cls._get_local()
        local.current_trace_id = trace_id
        local.current_span_id = current_span_id

    @classmethod
    def get_current_trace_id(cls) -> Optional[str]:
        local = cls._get_local()
        return getattr(local, "current_trace_id", None)

    @classmethod
    def get_current_span_id(cls) -> Optional[str]:
        local = cls._get_local()
        return getattr(local, "current_span_id", None)

    @classmethod
    def set_current_span(cls, span_id: str) -> None:
        local = cls._get_local()
        local.current_span_id = span_id

    @classmethod
    def reset(cls) -> None:
        local = cls._get_local()
        local.current_trace_id = None
        local.current_span_id = None


# ============================================================
# Tracer（便捷埋点 API）
# ============================================================

class Tracer:
    """便捷埋点类，封装 TraceManager 常用操作

    使用方式：
        tracer = Tracer(trace_manager)
        with tracer.span("llm_invoke", SpanType.LLM_INVOKE, parent_span_id=...):
            # 业务逻辑
            tracer.add_event("llm.request", {"model": "gpt-4"})
    """

    def __init__(self, trace_manager: TraceManager) -> None:
        self._tm = trace_manager

    def start_trace(self, session_id: str, user_query: str) -> Trace:
        """启动 trace"""
        trace = self._tm.start_trace(session_id, user_query)
        TraceContext.set_current_trace(trace.trace_id, trace.root_span_id)
        return trace

    def start_span(
        self,
        span_type: str,
        name: str,
        parent_span_id: Optional[str] = None,
    ) -> "_SpanContext":
        """启动一个 span，返回 SpanContext 用于 with 语句"""
        trace_id = TraceContext.get_current_trace_id()
        if trace_id is None:
            raise RuntimeError("No active trace. Call tracer.start_trace() first.")
        span = self._tm.start_span(
            parent_span_id=parent_span_id,
            trace_id=trace_id,
            span_type=span_type,
            name=name,
        )
        TraceContext.set_current_span(span.span_id)
        return _SpanContext(tracer=self, span=span)

    def add_event(self, event_name: str, payload: Optional[dict[str, Any]] = None) -> None:
        """添加事件到当前活跃 span"""
        span_id = TraceContext.get_current_span_id()
        if span_id:
            self._tm.add_span_event(span_id, event_name, _truncate_value(payload))

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        """给当前活跃 span 设置 attributes"""
        span_id = TraceContext.get_current_span_id()
        if span_id:
            span = self._tm.get_active_span(span_id)
            if span:
                span.attributes.update(_truncate_attributes(attributes))

    def end_trace(self, status: str = TraceStatus.SUCCESS.value, error_msg: Optional[str] = None) -> None:
        """结束当前 trace"""
        trace_id = TraceContext.get_current_trace_id()
        if trace_id:
            self._tm.end_trace(trace_id, status, error_msg)
            TraceContext.reset()


class _SpanContext:
    """Span 上下文管理器"""

    def __init__(self, tracer: Tracer, span: Span) -> None:
        self._tracer = tracer
        self._span = span
        self._span_id = span.span_id

    def __enter__(self) -> "_SpanContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        status = SpanStatus.OK.value
        error = None
        if exc_type is not None:
            status = SpanStatus.ERROR.value
            error = {
                "message": str(exc_val),
                "stack": traceback.format_exc(),
            }
        self._tracer._tm.end_span(self._span_id, status, error)
        # 恢复父 span
        if self._span.parent_span_id:
            TraceContext.set_current_span(self._span.parent_span_id)
        else:
            TraceContext.set_current_span(None)


# ============================================================
# Serialization helpers
# ============================================================

def _trace_to_dict(trace: Trace) -> dict[str, Any]:
    return {
        "trace_id": trace.trace_id,
        "session_id": trace.session_id,
        "user_query": trace.user_query,
        "start_time": trace.start_time,
        "end_time": trace.end_time,
        "total_duration_ms": trace.total_duration_ms,
        "status": trace.status,
        "error_msg": trace.error_msg,
        "root_span_id": trace.root_span_id,
    }


def _dict_to_trace(data: dict[str, Any]) -> Trace:
    return Trace(
        trace_id=data["trace_id"],
        session_id=data["session_id"],
        user_query=data.get("user_query", ""),
        start_time=data.get("start_time", 0),
        end_time=data.get("end_time", 0),
        total_duration_ms=data.get("total_duration_ms", 0),
        status=data.get("status", TraceStatus.SUCCESS.value),
        error_msg=data.get("error_msg"),
        root_span_id=data.get("root_span_id"),
    )


def _span_to_dict(span: Span) -> dict[str, Any]:
    return {
        "span_id": span.span_id,
        "trace_id": span.trace_id,
        "parent_span_id": span.parent_span_id,
        "span_type": span.span_type,
        "name": span.name,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ms": span.duration_ms,
        "status": span.status,
        "error": span.error,
        "attributes": _truncate_attributes(span.attributes),
        "events": span.events,
    }


def _dict_to_span(data: dict[str, Any]) -> Span:
    return Span(
        span_id=data["span_id"],
        trace_id=data["trace_id"],
        parent_span_id=data.get("parent_span_id"),
        span_type=data.get("span_type", ""),
        name=data.get("name", ""),
        start_time=data.get("start_time", 0),
        end_time=data.get("end_time", 0),
        duration_ms=data.get("duration_ms", 0),
        status=data.get("status", SpanStatus.OK.value),
        error=data.get("error"),
        attributes=data.get("attributes", {}),
        events=data.get("events", []),
    )


def _truncate_attributes(attrs: dict[str, Any], max_length: int = 2000) -> dict[str, Any]:
    """截断 attributes 中的超长值，避免存储爆炸"""
    result = {}
    for key, value in attrs.items():
        if isinstance(value, str) and len(value) > max_length:
            result[key] = value[:max_length] + "...[truncated]"
        elif isinstance(value, (list, dict)):
            try:
                serialized = json.dumps(value, ensure_ascii=False)
                if len(serialized) > max_length:
                    result[key] = serialized[:max_length] + "...[truncated]"
                else:
                    result[key] = value
            except (TypeError, ValueError):
                result[key] = str(value)[:max_length]
        else:
            result[key] = value
    return result


def _truncate_value(value: Any, max_length: int = 2000) -> Any:
    """递归截断值中的超长字符串"""
    if isinstance(value, str) and len(value) > max_length:
        return value[:max_length] + "...[truncated]"
    if isinstance(value, dict):
        return {k: _truncate_value(v, max_length) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_value(v, max_length) for v in value]
    return value


# ============================================================
# Time / UUID helpers
# ============================================================

def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


def _now_ms() -> int:
    return int(time.time() * 1000)
