"""
Transcript 会话持久化（jsonl 读写 + rewind 回滚快照）
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from mokioclaw.compact.types import CompactConfig, CompactState, TranscriptRecord
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class Transcript:
    """Session transcript 持久化，jsonl 格式。

    每行一条快照：{timestamp, raw_messages[], compact_state_snapshot{}}
    """

    def __init__(self, file_path: str | Path) -> None:
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, messages: list[Any], compact_state: CompactState) -> None:
        """写入快照"""
        record = {
            "timestamp": time.time(),
            "raw_messages": [_serialize_message(m) for m in messages],
            "compact_state_snapshot": {
                "has_compacted": compact_state.has_compacted,
                "last_boundary_index": compact_state.last_boundary_index,
                "retry_count": compact_state.retry_count,
                "recent_modified_files": compact_state.recent_modified_files,
            },
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def load_messages(self) -> list[Any]:
        """读取最新一次快照的原始 messages"""
        if not self._path.exists():
            return []

        last_messages: list[dict] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    last_messages = record.get("raw_messages", [])
                except json.JSONDecodeError:
                    continue

        return _deserialize_messages(last_messages)

    def rewind(self) -> Tuple[list[Any], CompactState]:
        """回滚到最近一次 compact 前的快照，撤销压缩。

        返回 (messages, compact_state)，如果找不到则返回 ([], CompactState())。
        """
        if not self._path.exists():
            return [], CompactState()

        last_pre_compact = None
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    cs = record.get("compact_state_snapshot", {})
                    if not cs.get("has_compacted", False):
                        last_pre_compact = record
                except json.JSONDecodeError:
                    continue

        if last_pre_compact is None:
            return [], CompactState()

        messages = _deserialize_messages(last_pre_compact.get("raw_messages", []))
        cs = last_pre_compact.get("compact_state_snapshot", {})
        state = CompactState(
            has_compacted=cs.get("has_compacted", False),
            last_boundary_index=cs.get("last_boundary_index", 0),
            retry_count=cs.get("retry_count", 0),
            recent_modified_files=cs.get("recent_modified_files", []),
        )
        return messages, state


# ============================================================
# 序列化辅助
# ============================================================

def _serialize_message(msg: Any) -> dict:
    """LangChain 消息 → dict"""
    if hasattr(msg, "to_dict"):
        return msg.to_dict()
    return {
        "type": type(msg).__name__,
        "content": str(getattr(msg, "content", None) or ""),
    }


def _deserialize_messages(data: list[dict]) -> list[Any]:
    """dict → LangChain 消息列表"""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    type_map = {
        "HumanMessage": HumanMessage,
        "AIMessage": AIMessage,
        "SystemMessage": SystemMessage,
        "ToolMessage": ToolMessage,
    }
    result: list[Any] = []
    for item in data:
        msg_type = item.get("type", "")
        cls = type_map.get(msg_type)
        if cls is None:
            continue
        kwargs: dict[str, Any] = {"content": item.get("content", "")}
        if msg_type == "ToolMessage":
            kwargs["tool_call_id"] = item.get("tool_call_id", "")
            kwargs["name"] = item.get("name", "")
        result.append(cls(**kwargs))
    return result
