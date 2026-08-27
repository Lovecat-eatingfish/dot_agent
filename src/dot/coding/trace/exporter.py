"""
dot.coding.trace.exporter — LocalFileTraceExporter

本地 JSONL 文件导出器。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LocalFileTraceExporter:
    """本地 JSONL 文件导出器"""

    def __init__(self, output_dir: Path) -> None:
        self._dir = output_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def export(self, record: dict[str, Any]) -> None:
        """导出一条 span 记录"""
        trace_id = record.get("trace_id", "unknown")
        path = self._dir / f"{trace_id}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("[trace] export failed: %s", exc)
