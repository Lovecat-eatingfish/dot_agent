"""
LocalFileTraceExporter — 本地 JSONL 追踪导出器（doc/fix-链路追踪.md）

  - 存储：<workspace>/.dot/traces/<YYYY-MM-DD>/trace_{session_id}.jsonl
    按天 + 会话拆分，程序异常退出不会损坏整个文件
  - 写入：queue.Queue + 后台守护线程，非阻塞 export，
    任何失败静默吞掉——追踪绝不影响主业务流程
  - 轮转：按天目录切割，自动清理 RETENTION_DAYS 天之前的历史
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..core.log import get_logger

logger = get_logger(__name__)

# 历史追踪保留天数（避坑 #2：必须做轮转/清理）
RETENTION_DAYS = 7


class LocalFileTraceExporter:
    """异步 JSONL 导出器"""

    def __init__(self, trace_dir: Path | str) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._worker = threading.Thread(target=self._write_loop, daemon=True, name="dot-trace")
        self._worker.start()
        self._last_cleanup_date = ""
        logger.info("[trace] LocalFileTraceExporter ready: %s", self.trace_dir)

    def export(self, span: dict[str, Any]) -> None:
        """对外上报接口（非阻塞）"""
        self._queue.put(span)

    # flush 栅栏：插入后等 daemon 处理到它（FIFO 保证栅栏前所有 span 已落盘）
    _FLUSH_SENTINEL = object()

    def flush(self, timeout: float = 5.0) -> None:
        """等待所有已入队 span 落盘（测试 / 优雅退出用）

        旧实现只查 queue.empty()，daemon 可能已 dequeue 但尚未写盘就返回，
        导致紧时序下读不到刚 finish 的 span。改用栅栏：FIFO 保证栅栏前的
        span 全部写完后 daemon 才处理栅栏、set 事件。
        """
        if not self._worker.is_alive():
            return
        done = threading.Event()
        self._queue.put((self._FLUSH_SENTINEL, done))
        done.wait(timeout=timeout)

    # ----------------------------------------------------------
    # Internal
    # ----------------------------------------------------------

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, tuple) and item and item[0] is self._FLUSH_SENTINEL:
                    item[1].set()  # 通知 flush：栅栏前的 span 已全部写完
                    continue
                span = item
                session_id = (span.get("tags") or {}).get("session_id") or "default"
                date_str = time.strftime("%Y-%m-%d")
                file_path = self.trace_dir / date_str / f"trace_{_safe_name(str(session_id))}.jsonl"
                if file_path.parent.exists() is False:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cleanup_old(date_str)
                with open(file_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(span, ensure_ascii=False, default=str) + "\n")
            except Exception as exc:
                # 追踪失败绝对不能影响主业务流程
                logger.debug("[trace] write failed: %s", exc)
            finally:
                self._queue.task_done()

    def _cleanup_old(self, today: str) -> None:
        """清理保留期之外的日期目录（每天首次建目录时执行一次）"""
        if self._last_cleanup_date == today:
            return
        self._last_cleanup_date = today
        try:
            cutoff = time.mktime(time.strptime(today, "%Y-%m-%d")) - RETENTION_DAYS * 86400
            for child in self.trace_dir.iterdir():
                if not child.is_dir():
                    continue
                try:
                    if time.mktime(time.strptime(child.name, "%Y-%m-%d")) < cutoff:
                        import shutil

                        shutil.rmtree(child, ignore_errors=True)
                        logger.info("[trace] cleaned old traces: %s", child.name)
                except ValueError:
                    continue  # 非日期目录
        except Exception as exc:
            logger.debug("[trace] cleanup failed: %s", exc)


def _safe_name(name: str) -> str:
    """session_id 转安全文件名片段"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "default"
