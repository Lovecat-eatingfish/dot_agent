"""Minimal event recorder: one JSONL file per trace."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.utils import json_safe, truncate, utc_now
from mokioclaw.reliability.cost import active_model_name


VALID_TRACE_MODES = {"on", "off"}
EXECUTIONS_ROOT = Path(".mokioclaw") / "executions"
EVENTS_FILE = "events.jsonl"
MAX_PAYLOAD_TEXT = 1200


def normalize_trace_mode(mode: str | None) -> str:
    normalized = (mode or "on").strip().lower()
    return normalized if normalized in VALID_TRACE_MODES else "on"


class TraceRecorder:
    def __init__(self, runtime: Any, task: str = "") -> None:
        self.runtime = runtime
        self.workspace = runtime.workspace
        self.mode = normalize_trace_mode(getattr(runtime, "trace_mode", "on"))
        self.trace_id = getattr(runtime, "trace_id", None) or _new_trace_id()
        self.task = task
        self.root = self.workspace / EXECUTIONS_ROOT / self.trace_id
        self.started_at = time.perf_counter()
        self.started_at_iso = utc_now()
        self.sequence = 0
        self.errors: list[str] = []
        self.status = "running"
        self.node_visits: dict[str, int] = {}
        self.tool_calls = 0
        self.failed_tool_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.approval_count = 0
        self.checkpoint_count = 0
        self.handoff_count = 0
        self.final_status = ""
        self.model_name = ""
        self.cost_usd = 0.0
        # 层级化链路追踪
        self._span_stack: list[str] = []
        self._span_counter = 0
        self.spans: list[dict[str, Any]] = []
        self._final_state: dict[str, Any] = {}
        if self.enabled:
            setattr(runtime, "trace_id", self.trace_id)
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def start(self, inputs: dict[str, Any], *, resumed: bool = False, resume_event: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self.record(
            "run_start",
            {
                "task": inputs.get("task", self.task),
                "workspace": str(self.workspace),
                "resumed": resumed,
                "resume": resume_event or {},
                "max_attempts": inputs.get("max_attempts"),
                "checkpoint_mode": getattr(self.runtime, "checkpoint_mode", ""),
            },
        )

    def record_token_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        if not self.enabled:
            return
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.record("token_usage", {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": self.total_tokens})

    def start_span(self, node: str, *, span_type: str = "node") -> str:
        if not self.enabled:
            return ""
        self._span_counter += 1
        span_id = f"span-{self._span_counter:04d}"
        parent_id = self._span_stack[-1] if self._span_stack else None
        self._span_stack.append(span_id)
        span_meta = {
            "span_id": span_id,
            "parent_span_id": parent_id,
            "node": node,
            "span_type": span_type,
            "started_at": utc_now(),
            "start_elapsed_ms": self.elapsed_ms(),
        }
        self.spans.append(span_meta)
        self.record("span_start", {
            "span_id": span_id,
            "parent_span_id": parent_id,
            "node": node,
            "span_type": span_type,
        })
        return span_id

    def end_span(self, node: str) -> None:
        if not self.enabled or not self._span_stack:
            return
        span_id = self._span_stack.pop()
        for span in self.spans:
            if span.get("span_id") == span_id:
                span["ended_at"] = utc_now()
                span["duration_ms"] = self.elapsed_ms() - span.get("start_elapsed_ms", 0)
                break
        self.record("span_end", {
            "span_id": span_id,
            "node": node,
            "duration_ms": self.elapsed_ms(),
        })

    def record_custom_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        event_type = str(event.get("type", "custom_event"))
        if event_type == "tool_call":
            self.tool_calls += 1
        elif event_type == "tool_result":
            result = event.get("result")
            if isinstance(result, dict):
                if result.get("ok") is False:
                    self.failed_tool_calls += 1
                if result.get("requires_approval"):
                    self.approval_count += 1
        elif event_type == "handoff":
            self.handoff_count += 1
        elif event_type == "checkpoint_saved":
            self.checkpoint_count += 1

        recorded_event = dict(event)
        if event_type == "tool_call" and isinstance(recorded_event.get("args"), dict):
            recorded_event["args"] = _sanitize_args(recorded_event["args"])
        self.record(
            f"custom:{event_type}",
            {
                "event_type": event_type,
                "node": event.get("node") or event.get("from") or "",
                "name": event.get("name") or "",
                "payload": compact_payload(recorded_event),
            },
        )

    def record_graph_update(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        nodes = list(event.keys()) if isinstance(event, dict) else []
        for node in nodes:
            self.node_visits[node] = self.node_visits.get(node, 0) + 1
            if node == "final":
                update = event.get(node)
                if isinstance(update, dict):
                    self.final_status = "passed" if "PASSED" in str(update.get("final_answer", "")) else "failed"

        for node in nodes:
            if node != "final":
                self.start_span(node, span_type="graph_node")

        self.record("graph_update", {"nodes": nodes, "payload": compact_payload(event)})

        for node in nodes:
            if node != "final":
                self.end_span(node)

    def end(self, *, status: str, latest_node: str = "", final_state: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self.status = status
        self._final_state = final_state or {}
        self.record("run_end", {
            "status": status,
            "latest_node": latest_node,
            "attempts": self._final_state.get("attempts"),
            "passed": self._final_state.get("passed"),
            "final_status": self.final_status,
            "node_visits": dict(sorted(self.node_visits.items())),
            "tool_calls": self.tool_calls,
        })

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.sequence += 1
            line = {
                "seq": self.sequence,
                "timestamp": utc_now(),
                "elapsed_ms": self.elapsed_ms(),
                "type": event_type,
                "payload": compact_payload(payload),
            }
            with (self.root / EVENTS_FILE).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def summary_payload(self) -> dict[str, Any]:
        final_state = getattr(self, "_final_state", {}) or {}
        status = "PASSED" if final_state.get("passed") else "FAILED"
        parts = [
            f"{status} after {final_state.get('attempts', 0)} attempt(s)",
            f"trace_id={self.trace_id}",
            f"workspace={self.workspace}",
        ]
        if final_state.get("plan_summary"):
            parts.append(f"plan={truncate(str(final_state.get('plan_summary', '')), 300)}")
        if final_state.get("verifier_summary"):
            parts.append(f"verifier={truncate(str(final_state.get('verifier_summary', '')), 300)}")
        if final_state.get("repair_instruction"):
            parts.append(f"repair={truncate(str(final_state.get('repair_instruction', '')), 300)}")
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "final_status": self.final_status,
            "summary": " | ".join(parts),
            "workspace": str(self.workspace),
            "trace_dir": str(self.root),
            "events_file": str(self.root / EVENTS_FILE),
            "started_at": self.started_at_iso,
            "ended_at": utc_now(),
            "duration_ms": self.elapsed_ms(),
            "event_count": self.sequence,
            "node_visits": dict(sorted(self.node_visits.items())),
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model_name or active_model_name(),
            "cost_usd": self.cost_usd,
            "approval_count": self.approval_count,
            "checkpoint_count": self.checkpoint_count,
            "handoff_count": self.handoff_count,
            "errors": list(self.errors),
            "spans": self.spans,
            "final_state": self._final_state,
        }

    def elapsed_ms(self) -> int:
        return round((time.perf_counter() - self.started_at) * 1000)


def compact_payload(value: Any, *, limit: int = MAX_PAYLOAD_TEXT) -> Any:
    safe = json_safe(value)
    return _trim_nested(safe, limit=limit)


def _trim_nested(value: Any, *, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 3] + "..."
    if isinstance(value, dict):
        return {key: _trim_nested(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim_nested(item, limit=limit) for item in value[:80]]
    return value


def _new_trace_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"trace-{stamp}-{uuid4().hex[:6]}"


_SENSITIVE_KEY_PATTERNS = re.compile(r"key|token|secret|password|credential|auth", re.IGNORECASE)


def _sanitize_args(args: Any) -> Any:
    if not isinstance(args, dict):
        return args
    sanitized: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and _SENSITIVE_KEY_PATTERNS.search(key):
            sanitized[key] = "***"
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_args(value)
        else:
            sanitized[key] = value
    return sanitized
