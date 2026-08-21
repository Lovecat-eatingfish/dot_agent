"""
mokioclaw.compact — 五层渐进式上下文压缩管理器

公开 API:
    apply_compression_pipeline  主入口，五层流水线
    CompactConfig / CompactState 配置与状态
    Transcript                   会话持久化 + rewind
"""
from __future__ import annotations

from mokioclaw.compact.types import CompactConfig, CompactState, TranscriptRecord
from mokioclaw.compact.compact import apply_compression_pipeline
from mokioclaw.compact.transcript import Transcript

__all__ = [
    "CompactConfig",
    "CompactState",
    "TranscriptRecord",
    "apply_compression_pipeline",
    "Transcript",
]
