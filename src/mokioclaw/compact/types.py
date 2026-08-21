"""
数据类型定义
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class CompactConfig:
    """压缩配置"""

    max_context_window: int = 200_000
    output_reserve: int = 8_192
    safety_buffer: int = 10_000
    recent_retain_turns: int = 2
    max_compact_retry: int = 3
    enable_snip: bool = True


@dataclass
class CompactState:
    """压缩状态（跨轮持久化）"""

    has_compacted: bool = False
    last_boundary_index: int = 0
    retry_count: int = 0
    recent_modified_files: List[str] = field(default_factory=list)


@dataclass
class TranscriptRecord:
    """Transcript 单条快照"""

    timestamp: float
    raw_messages: List[Any]  # LangChain BaseMessage 列表
    compact_state_snapshot: CompactState
