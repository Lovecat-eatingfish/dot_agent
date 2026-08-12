"""
内置 Hook 实现

提供开箱即用的事件处理器，订阅 EventBus 并执行副作用：
- TuiPushHook: 推送事件到 TUI 界面
- CliPrintHook: 在 Rich CLI 模式下打印事件
- CheckpointHook: 在关键节点触发 checkpoint 保存
- TraceHook: 写入 trace events.jsonl
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from mokioclaw.core.events import EventBus, get_event_bus
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# TUI 推送 Hook
# ---------------------------------------------------------------------------

class TuiPushHook:
    """将事件推送到 TUI 应用

    将 event dict 序列化后通过 TUI 的 post_message 发送。
    需要 TUI 应用注册对应的消息处理器。
    """

    def __init__(self, push_callback: Callable[[dict[str, Any]], None]) -> None:
        self._push = push_callback
        self._unsub: Callable[[], None] | None = None
        self._enabled = True

    def start(self) -> None:
        bus = get_event_bus()
        self._unsub = bus.subscribe_all(self._on_event, priority=10)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on_event(self, event: dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            self._push(event)
        except Exception as exc:
            logger.debug("TUI push hook error: %s", exc)


# ---------------------------------------------------------------------------
# CLI 打印 Hook
# ---------------------------------------------------------------------------

class CliPrintHook:
    """在 Rich CLI 模式下打印事件摘要"""

    def __init__(self, print_func: Callable[[str], None] | None = None) -> None:
        self._print = print_func or (lambda msg: None)
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        bus = get_event_bus()
        self._unsub = bus.subscribe_all(self._on_event, priority=0)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        if event_type == "tool_call":
            self._print(f"[tool] {event.get('node', '?')}.{event.get('name', '?')}")
        elif event_type == "tool_result":
            result = event.get("result", {})
            ok = result.get("ok") if isinstance(result, dict) else None
            status = "ok" if ok else "FAIL"
            self._print(f"[tool] {event.get('node', '?')}.{event.get('name', '?')} → {status}")
        elif event_type == "handoff":
            self._print(f"[handoff] {event.get('from', '?')} → {event.get('to', '?')}: {event.get('instruction', '')[:60]}")
        elif event_type == "context_compression":
            self._print(f"[compress] {event.get('before_tokens', 0)} → {event.get('after_tokens', 0)} tokens")
        elif event_type == "checkpoint_saved":
            self._print(f"[checkpoint] saved at {event.get('path', '?')}")


# ---------------------------------------------------------------------------
# Checkpoint 触发 Hook
# ---------------------------------------------------------------------------

class CheckpointHook:
    """在关键节点触发 checkpoint 保存

    监听 checkpoint_saved 事件之外的关键节点，
    在 planner、verifier、final 节点后自动保存轻量 checkpoint。
    """

    def __init__(self, save_func: Callable[[str], None]) -> None:
        self._save = save_func
        self._unsub_plan: Callable[[], None] | None = None
        self._unsub_final: Callable[[], None] | None = None
        self._saved_nodes: set[str] = set()

    def start(self) -> None:
        bus = get_event_bus()
        self._unsub_plan = bus.subscribe("plan_snapshot", self._on_plan, priority=5)
        self._unsub_final = bus.subscribe("final", self._on_final, priority=5)

    def stop(self) -> None:
        for name in ("_unsub_plan", "_unsub_final"):
            unsub = getattr(self, name, None)
            if unsub is not None:
                unsub()
                setattr(self, name, None)

    def _on_plan(self, event: dict[str, Any]) -> None:
        node = event.get("node", "")
        if node not in self._saved_nodes:
            self._saved_nodes.add(node)
            self._save(f"planner-{node}")

    def _on_final(self, event: dict[str, Any]) -> None:
        self._save("final")


# ---------------------------------------------------------------------------
# Trace 写入 Hook
# ---------------------------------------------------------------------------

class TraceHook:
    """将事件写入 trace events.jsonl"""

    def __init__(self, trace_dir: str) -> None:
        self._trace_dir = trace_dir
        self._unsub: Callable[[], None] | None = None
        self._file = None
        self._enabled = True

    def start(self) -> None:
        bus = get_event_bus()
        self._unsub = bus.subscribe_all(self._on_event, priority=0)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def _on_event(self, event: dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            if self._file is None:
                os.makedirs(self._trace_dir, exist_ok=True)
                path = os.path.join(self._trace_dir, "events.jsonl")
                self._file = open(path, "a", encoding="utf-8")
            self._file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._file.flush()
        except Exception as exc:
            logger.debug("Trace hook error: %s", exc)
