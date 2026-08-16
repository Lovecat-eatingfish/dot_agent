"""
记忆读写工具 — 显式操作 MEMORY 主题文件
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.memory.topic_store import TOPIC_TYPES, TopicStore
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools.registry import TOOL_CONCURRENCY_META


def build_memory_tools(state: RuntimeState) -> list[StructuredTool]:
    store = TopicStore(state.workspace)

    def _read(name: str) -> dict[str, Any]:
        return store.read_topic(name)

    def _write(name: str, content: str, topic_type: str = "project", description: str = "") -> dict[str, Any]:
        if topic_type not in TOPIC_TYPES:
            topic_type = "project"
        result = store.write_topic(name, content, topic_type=topic_type, description=description)
        safe_name = result.get("name") or name
        try:
            path = state.workspace / ".mokioclaw" / "memory" / f"{safe_name}.md"
            if path.exists():
                state.record_read(path, complete=True)
        except Exception:
            pass
        return result

    def _index() -> dict[str, Any]:
        return {"ok": True, "index": store.load_index(), "topics": [t.to_dict() if hasattr(t, "to_dict") else {
            "name": t.name, "description": t.description, "type": t.topic_type
        } for t in store.list_topics()]}

    TOOL_CONCURRENCY_META["MemoryReadTool"] = True
    TOOL_CONCURRENCY_META["MemoryWriteTool"] = False
    TOOL_CONCURRENCY_META["MemoryIndexTool"] = True

    return [
        StructuredTool.from_function(
            name="MemoryIndexTool",
            func=_index,
            description="Read MEMORY.md index and list available memory topics (progressive disclosure).",
        ),
        StructuredTool.from_function(
            name="MemoryReadTool",
            func=_read,
            description="Read one memory topic file by name (without .md).",
        ),
        StructuredTool.from_function(
            name="MemoryWriteTool",
            func=_write,
            description=(
                "Write/update a memory topic and refresh MEMORY.md index. "
                "Args: name, content, optional topic_type (user/feedback/project/reference), description."
            ),
        ),
    ]
