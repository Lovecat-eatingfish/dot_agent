"""
后台子 Agent 任务注册表

对齐 Claude Code BackgroundTask：
- Agent(run_in_background=true) 立刻返回 taskId
- 主 Agent 用 BackgroundTaskStatus / Cancel 轮询或取消
- 子 Agent 结果只存在进程内存，不自动推入主 messages
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class BackgroundTask:
    task_id: str
    status: str = "running"  # running | completed | error | cancelled
    description: str = ""
    final_report: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cancelled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class BackgroundTaskRegistry:
    """进程内后台任务表（线程安全）"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.RLock()

    def create(self, description: str = "", **meta: Any) -> BackgroundTask:
        task_id = f"bg-{uuid.uuid4().hex[:12]}"
        task = BackgroundTask(task_id=task_id, description=description, meta=dict(meta))
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks.values())

    def complete(self, task_id: str, report: str) -> BackgroundTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.cancelled:
                task.status = "cancelled"
            else:
                task.status = "completed"
                task.final_report = report
            task.touch()
            return task

    def fail(self, task_id: str, error: str) -> BackgroundTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = "error"
            task.error = error
            task.touch()
            return task

    def cancel(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.cancelled = True
            if task.status == "running":
                task.status = "cancelled"
            task.touch()
            return task

    def to_status_dict(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            return {"ok": False, "error": f"unknown task_id: {task_id}"}
        return {
            "ok": True,
            "task_id": task.task_id,
            "status": task.status,
            "description": task.description,
            "final_report": task.final_report,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }


_REGISTRY: BackgroundTaskRegistry | None = None
_REG_LOCK = threading.Lock()


def get_background_registry() -> BackgroundTaskRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REG_LOCK:
            if _REGISTRY is None:
                _REGISTRY = BackgroundTaskRegistry()
    return _REGISTRY


def reset_background_registry() -> None:
    global _REGISTRY
    with _REG_LOCK:
        _REGISTRY = BackgroundTaskRegistry()


def run_in_thread(fn: Callable[[], None]) -> None:
    """启动守护线程执行后台工作"""
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
