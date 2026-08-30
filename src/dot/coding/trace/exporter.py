"""
dot.coding.trace.exporter — LocalFileTraceExporter

本地 JSONL 文件导出器：queue.Queue + 后台守护线程异步写入，
导出失败静默吞掉——追踪绝不阻塞或中断主链路。

文件布局：<output_dir>/<YYYY-MM-DD>/trace_{session_id}.jsonl
按天 + 会话拆分：同一天同一 session 的 span 追加到同一文件。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LocalFileTraceExporter:
    """异步本地 JSONL 导出器"""

    def __init__(self, output_dir: Path) -> None:
        self._dir = output_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="dot-trace")
        self._thread.start()

    @property
    def dir(self) -> Path:
        return self._dir

    def export(self, record: dict[str, Any]) -> None:
        """入队一条 span 记录（非阻塞）"""
        try:
            self._queue.put_nowait(record)
        except Exception:
            logger.debug("[trace] export enqueue failed")

    def wait_flushed(self) -> None:
        """阻塞直到队列中所有记录落盘（测试与进程退出前调用）"""
        self._queue.join()

    def _worker(self) -> None:
        """后台写盘线程：逐条追加到按天/会话命名的 JSONL"""
        while True:
            record = self._queue.get()
            if record is None:
                return
            path = self._file_for(record)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.debug("[trace] export failed: %s", exc)
            finally:
                self._queue.task_done()

    def _file_for(self, record: dict[str, Any]) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_id = record.get("tags", {}).get("session_id") or record.get("trace_id", "unknown")
        day_dir = self._dir / day
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return day_dir / f"trace_{session_id}.jsonl"

    def close(self) -> None:
        """停止后台线程（进程退出时可调用；daemon 线程不调用也会随进程结束）"""
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
