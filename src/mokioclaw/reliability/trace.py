from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.utils import json_safe, truncate, utc_now, write_json


VALID_TRACE_MODES = {"on", "off"}
TRACE_ROOT = Path(".mokioclaw") / "traces"
EVENTS_FILE = "events.jsonl"
SUMMARY_FILE = "summary.json"
TIMELINE_FILE = "timeline.md"
MAX_PAYLOAD_TEXT = 1200
TIMELINE_HEAD_ITEMS = 40
TIMELINE_TAIL_ITEMS = 80


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
        self.root = self.workspace / TRACE_ROOT / self.trace_id
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
        self.timeline_head: list[str] = []
        self.timeline_tail: list[str] = []
        self.timeline_omitted = 0
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
        elif event_type == "checkpoint_resumed":
            self._timeline(f"resume {event.get('mode', '')} fallback={event.get('fallback', False)}")

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
        self.record(
            "graph_update",
            {
                "nodes": nodes,
                "payload": compact_payload(event),
            },
        )

    def end(self, *, status: str, latest_node: str = "", final_state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        self.status = status
        payload = {
            "status": status,
            "latest_node": latest_node,
            "attempts": (final_state or {}).get("attempts"),
            "passed": (final_state or {}).get("passed"),
            "final_status": self.final_status,
            "plan_summary": truncate(str((final_state or {}).get("plan_summary", "")), 600),
            "verifier_summary": truncate(str((final_state or {}).get("verifier_summary", "")), 600),
            "repair_instruction": truncate(str((final_state or {}).get("repair_instruction", "")), 600),
            "acceptance_criteria": (final_state or {}).get("acceptance_criteria", []),
            "verification_checks": (final_state or {}).get("verification_checks", []),
        }
        self.final_state_summary = payload
        self.record("run_end", payload)
        return self.write_summary()

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
            self._timeline(format_timeline_line(line))
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def write_summary(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        summary = self.summary_payload()
        try:
            write_json(self.root / SUMMARY_FILE, summary)
            (self.root / TIMELINE_FILE).write_text(build_timeline_markdown(summary, self.timeline_items()), encoding="utf-8")
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            summary = self.summary_payload()
        return trace_summary_event(summary)

    def summary_payload(self) -> dict[str, Any]:
        final_state = getattr(self, "final_state_summary", {}) or {}
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "final_status": self.final_status,
            "summary": self._build_summary_text(final_state),
            "workspace": str(self.workspace),
            "trace_dir": str(self.root),
            "events_file": str(self.root / EVENTS_FILE),
            "summary_file": str(self.root / SUMMARY_FILE),
            "timeline_file": str(self.root / TIMELINE_FILE),
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
            "approval_count": self.approval_count,
            "checkpoint_count": self.checkpoint_count,
            "handoff_count": self.handoff_count,
            "final_state": final_state,
            "errors": list(self.errors),
            "timeline_omitted": self.timeline_omitted,
        }

    def _build_summary_text(self, final_state: dict[str, Any] | None = None) -> str:
        state = final_state or {}
        status = "PASSED" if state.get("passed") else "FAILED"
        parts = [
            f"{status} after {state.get('attempts', 0)} attempt(s)",
            f"trace_id={self.trace_id}",
            f"workspace={self.workspace}",
        ]
        if state.get("plan_summary"):
            parts.append(f"plan={truncate(str(state.get('plan_summary', '')), 300)}")
        if state.get("verifier_summary"):
            parts.append(f"verifier={truncate(str(state.get('verifier_summary', '')), 300)}")
        if state.get("repair_instruction"):
            parts.append(f"repair={truncate(str(state.get('repair_instruction', '')), 300)}")
        return " | ".join(parts)

    def elapsed_ms(self) -> int:
        return round((time.perf_counter() - self.started_at) * 1000)

    def _timeline(self, text: str) -> None:
        if len(self.timeline_head) < TIMELINE_HEAD_ITEMS:
            self.timeline_head.append(text)
            return
        if len(self.timeline_tail) >= TIMELINE_TAIL_ITEMS:
            self.timeline_tail.pop(0)
            self.timeline_omitted += 1
        self.timeline_tail.append(text)

    def timeline_items(self) -> list[str]:
        if self.timeline_omitted <= 0:
            return self.timeline_head + self.timeline_tail
        return self.timeline_head + [f"... omitted {self.timeline_omitted} event(s) ..."] + self.timeline_tail


def trace_summary_event(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "trace_summary",
        **summary,
    }


def compact_payload(value: Any, *, limit: int = MAX_PAYLOAD_TEXT) -> Any:
    safe = json_safe(value)
    return _trim_nested(safe, limit=limit)




def format_timeline_line(line: dict[str, Any]) -> str:
    event_type = str(line.get("type", "event"))
    payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
    if event_type == "graph_update":
        nodes = ", ".join(payload.get("nodes", []))
        return f"{line.get('elapsed_ms')}ms graph_update nodes={nodes}"
    if event_type.startswith("custom:"):
        event_name = payload.get("event_type", event_type.removeprefix("custom:"))
        node = payload.get("node", "")
        name = payload.get("name", "")
        suffix = " ".join(part for part in [str(node), str(name)] if part)
        return f"{line.get('elapsed_ms')}ms {event_name} {suffix}".rstrip()
    return f"{line.get('elapsed_ms')}ms {event_type}"


def build_timeline_markdown(summary: dict[str, Any], timeline: list[str]) -> str:
    lines = [
        "# MokioClaw Trace Timeline",
        "",
        f"- trace_id: {summary.get('trace_id', '')}",
        f"- status: {summary.get('status', '')}",
        f"- duration_ms: {summary.get('duration_ms', 0)}",
        f"- workspace: {summary.get('workspace', '')}",
        f"- events: {summary.get('event_count', 0)}",
        "",
        "## Summary",
        "",
        f"- summary: {summary.get('summary', '')}",
        f"- nodes: {summary.get('node_visits', {})}",
        f"- tool_calls: {summary.get('tool_calls', 0)}",
        f"- failed_tool_calls: {summary.get('failed_tool_calls', 0)}",
        f"- approvals: {summary.get('approval_count', 0)}",
        f"- checkpoints: {summary.get('checkpoint_count', 0)}",
        f"- final_status: {summary.get('final_status', '')}",
        "",
        "## Final State",
        "",
        _render_final_state(summary.get('final_state', {})),
        "",
        "## Timeline",
        "",
    ]
    lines.extend(f"- {item}" for item in timeline)
    if not timeline:
        lines.append("- (none)")
    return "\n".join(lines).rstrip() + "\n"


def normalize_trace_path(path: Path) -> Path:
    return path.resolve()


def _render_final_state(final_state: dict[str, Any]) -> str:
    if not isinstance(final_state, dict) or not final_state:
        return "(no final state recorded)"
    lines = [
        f"- passed: {final_state.get('passed', '')}",
        f"- attempts: {final_state.get('attempts', '')}",
        f"- plan_summary: {truncate(str(final_state.get('plan_summary', '') or ''), 350) or '(none)'}",
        f"- verifier_summary: {truncate(str(final_state.get('verifier_summary', '') or ''), 350) or '(none)'}",
        f"- repair_instruction: {truncate(str(final_state.get('repair_instruction', '') or ''), 350) or '(none)'}",
        f"- acceptance_criteria: {len(final_state.get('acceptance_criteria', []) or [])}",
        f"- verification_checks: {len(final_state.get('verification_checks', []) or [])}",
    ]
    return "\n".join(lines)


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
    """对工具调用参数做简单脱敏，替换敏感字段值为 ***"""
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
