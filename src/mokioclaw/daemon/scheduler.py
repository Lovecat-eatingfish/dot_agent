"""
定时任务调度器

基于 cron 表达式的任务调度系统：
- cron 表达式解析与触发
- 任务持久化到 JSON 文件
- 后台线程定时扫描
- 任务状态追踪（pending/running/done/failed）
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# ============================================================
# cron 表达式解析
# ============================================================

# 简化版 cron 解析器（支持 5 字段标准格式）
_CRON_FIELD_PATTERN = re.compile(r"^(\*|\d+(-\d+)?(,\d+(-\d+)?)*|\*\/\d+|\d+\/\d+)$")


class CronSchedule:
    """cron 表达式解析与匹配

    标准 5 字段：minute hour day-of-month month day-of-week

    支持格式：
    - `*` — 任意值
    - `5` — 精确值
    - `1-5` — 范围
    - `1,3,5` — 列表
    - `*/10` — 步长
    - `1-5/2` — 范围 + 步长
    """

    def __init__(self, expression: str) -> None:
        self._expression = expression.strip()
        fields = self._expression.split()
        if len(fields) != 5:
            raise ValueError(f"Invalid cron expression: '{expression}'. Expected 5 fields.")
        self._minutes = self._parse_field(fields[0], 0, 59)
        self._hours = self._parse_field(fields[1], 0, 23)
        self._days_of_month = self._parse_field(fields[2], 1, 31)
        self._months = self._parse_field(fields[3], 1, 12)
        self._days_of_week = self._parse_field(fields[4], 0, 6)

    def matches(self, dt: datetime) -> bool:
        """检查给定时间是否匹配 cron 表达式

        Note: cron 的 day-of-week: 0=Sunday, 1=Monday, ..., 6=Saturday
        Python weekday(): 0=Monday, ..., 6=Sunday
        这里做转换：python_weekday + 1 mod 7 → cron_dow
        """
        python_dow = dt.weekday()  # 0=Mon ... 6=Sun
        cron_dow = (python_dow + 1) % 7  # 0=Sun ... 6=Sat
        return (
            dt.minute in self._minutes
            and dt.hour in self._hours
            and dt.day in self._days_of_month
            and dt.month in self._months
            and cron_dow in self._days_of_week
        )

    def next_run(self, after: datetime) -> datetime | None:
        """计算下一个匹配时间（从 after 开始，最多向前 2 年）"""
        current = after.replace(second=0, microsecond=0) + __import__("datetime").timedelta(minutes=1)
        deadline = current.replace(year=current.year + 2)
        while current <= deadline:
            if self.matches(current):
                return current
            current += __import__("datetime").timedelta(minutes=1)
        return None

    def _parse_field(self, field: str, min_val: int, max_val: int) -> set[int]:
        """解析单个 cron 字段"""
        # 早期拒绝明显畸形的字段
        if not _CRON_FIELD_PATTERN.match(field):
            raise ValueError(f"Invalid cron field: '{field}'")
        values: set[int] = set()

        if field == "*":
            return set(range(min_val, max_val + 1))

        for part in field.split(","):
            if "/" in part:
                range_part, _, step_str = part.partition("/")
                step = int(step_str)
                if range_part == "*":
                    start, end = min_val, max_val
                elif "-" in range_part:
                    start_str, _, end_str = range_part.partition("-")
                    start, end = int(start_str), int(end_str)
                else:
                    start = int(range_part)
                    end = max_val
                values.update(range(start, end + 1, step))
            elif "-" in part:
                start_str, _, end_str = part.partition("-")
                start, end = int(start_str), int(end_str)
                values.update(range(start, end + 1))
            else:
                values.add(int(part))

        return {v for v in values if min_val <= v <= max_val}

    def __str__(self) -> str:
        return self._expression


# ============================================================
# 任务定义与状态
# ============================================================

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@dataclass
class ScheduledTask:
    """定时任务定义"""
    id: str = ""
    name: str = ""
    cron: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    status: str = "pending"
    last_run: str = ""
    last_result: str = ""
    run_count: int = 0
    failure_count: int = 0
    max_failures: int = 5
    created_at: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cron": self.cron,
            "command": self.command,
            "args": list(self.args),
            "status": self.status,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "max_failures": self.max_failures,
            "created_at": self.created_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            cron=data.get("cron", ""),
            command=data.get("command", ""),
            args=list(data.get("args", [])),
            status=data.get("status", "pending"),
            last_run=data.get("last_run", ""),
            last_result=data.get("last_result", ""),
            run_count=data.get("run_count", 0),
            failure_count=data.get("failure_count", 0),
            max_failures=data.get("max_failures", 5),
            created_at=data.get("created_at", ""),
            description=data.get("description", ""),
        )


# ============================================================
# 调度器核心
# ============================================================

class CronScheduler:
    """定时任务调度器

    功能：
    - 添加/移除/更新定时任务
    - 后台线程扫描 cron 触发
    - 任务持久化到 JSON
    - 任务执行回调

    使用方式：
        scheduler = CronScheduler(tasks_dir=Path(".mokioclaw/tasks"))
        scheduler.add_task(ScheduledTask(name="daily-check", cron="0 9 * * *", command="echo hello"))
        scheduler.start()
        # ... later ...
        scheduler.stop()
    """

    def __init__(self, tasks_dir: Path | None = None, check_interval: float = 30.0) -> None:
        self._tasks_dir = tasks_dir or Path.cwd() / _DAEMON_DIR_NAME / "tasks"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._tasks_file = self._tasks_dir / "tasks.json"
        self._tasks: dict[str, ScheduledTask] = {}
        self._check_interval = check_interval
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._on_task_run: Callable[[ScheduledTask], None] | None = None
        self._load_tasks()

    @property
    def tasks(self) -> list[ScheduledTask]:
        with self._lock:
            return list(self._tasks.values())

    def add_task(self, task: ScheduledTask) -> str:
        """添加定时任务"""
        if not task.id:
            task.id = _gen_task_id()
        if not task.created_at:
            task.created_at = _now_iso()
        with self._lock:
            self._tasks[task.id] = task
        self._save_tasks()
        logger.info("Task added: %s (cron=%s)", task.id, task.cron)
        return task.id

    def remove_task(self, task_id: str) -> bool:
        """移除定时任务"""
        with self._lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
        self._save_tasks()
        logger.info("Task removed: %s", task_id)
        return True

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """获取单个任务"""
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task_id: str, **kwargs: Any) -> bool:
        """更新任务属性"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
        self._save_tasks()
        return True

    def set_run_callback(self, callback: Callable[[ScheduledTask], None]) -> None:
        """设置任务触发时的回调"""
        self._on_task_run = callback

    def start(self) -> None:
        """启动调度器（后台线程）"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cron-scheduler")
        self._thread.start()
        logger.info("Cron scheduler started (check_interval=%.1fs)", self._check_interval)

    def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Cron scheduler stopped")

    def run_pending(self) -> list[ScheduledTask]:
        """同步执行所有待触发的任务（用于测试/手动触发）"""
        triggered = []
        now = datetime.now(timezone.utc)
        with self._lock:
            for task in list(self._tasks.values()):
                if task.status != "pending":
                    continue
                if task.run_count > 0:
                    continue
                try:
                    schedule = CronSchedule(task.cron)
                except ValueError:
                    continue
                if schedule.matches(now):
                    triggered.append(task)
        return triggered

    # ============================================================
    # 内部方法
    # ============================================================

    def _run_loop(self) -> None:
        """后台调度循环"""
        last_check = datetime.now(timezone.utc)
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                # 每分钟检查一次（间隔内最多触发一次）
                if (now - last_check).total_seconds() >= 60:
                    self._check_triggers(now)
                    last_check = now
            except Exception as exc:
                logger.error("Scheduler loop error: %s", exc)
            time.sleep(self._check_interval)

    def _check_triggers(self, now: datetime) -> None:
        """检查并触发到期的任务"""
        with self._lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            if task.status != "pending":
                if task.status == "failed":
                    logger.debug(
                        "Task %s is failed (failures=%d/%d), skipping. "
                        "Use schedule-reset to retry.",
                        task.id, task.failure_count, task.max_failures,
                    )
                continue
            try:
                schedule = CronSchedule(task.cron)
            except ValueError:
                logger.warning("Invalid cron for task %s: %s", task.id, task.cron)
                continue
            if schedule.matches(now):
                self._execute_task(task)

    def _execute_task(self, task: ScheduledTask) -> None:
        """执行任务"""
        task.status = "running"
        task.last_run = _now_iso()
        self._save_tasks()
        logger.info("Task triggered: %s (%s)", task.id, task.name)

        if self._on_task_run is not None:
            try:
                self._on_task_run(task)
                task.status = "pending"  # 成功后重置，等待下次触发
                task.last_result = "ok"
                task.failure_count = 0  # 成功后重置失败计数
            except Exception as exc:
                task.status = "failed"
                task.failure_count += 1
                task.last_result = f"error: {exc}"
                logger.error("Task %s failed (attempt %d/%d): %s",
                           task.id, task.failure_count, task.max_failures, exc)
        else:
            task.status = "pending"
            task.last_result = "no handler"

        task.run_count += 1
        self._save_tasks()

    def _load_tasks(self) -> None:
        """从文件加载任务"""
        if not self._tasks_file.exists():
            return
        try:
            data = json.loads(self._tasks_file.read_text(encoding="utf-8"))
            tasks = data if isinstance(data, list) else data.get("tasks", [])
            for item in tasks:
                task = ScheduledTask.from_dict(item)
                if task.id:
                    self._tasks[task.id] = task
        except Exception as exc:
            logger.error("Failed to load tasks: %s", exc)

    def _save_tasks(self) -> None:
        """持久化任务到文件"""
        try:
            self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
            payload = [task.to_dict() for task in self._tasks.values()]
            self._tasks_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Failed to save tasks: %s", exc)


# ============================================================
# 辅助函数
# ============================================================

def _gen_task_id() -> str:
    return f"task-{int(time.time() * 1000) % 1000000:06d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 模块级单例
_scheduler: CronScheduler | None = None


def get_scheduler(tasks_dir: Path | None = None) -> CronScheduler:
    """获取全局调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler(tasks_dir=tasks_dir)
    return _scheduler


def reset_scheduler() -> None:
    """重置调度器单例"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
    _scheduler = None
