"""RAG 专用 trace：每请求唯一 trace_id 贯穿全程

设计（对齐 reliability/trace 风格但独立，不强耦合 runtime/workspace）：
- trace_id 生成复用 trace-<stamp>-<uuid6> 风格
- 每步记录 step/hits/scores/ms/degraded，落 .mokioclaw/rag/traces/{trace_id}.jsonl
- 生产只打关键日志；DEBUG 模式可查完整链路
- 所有错误必带 trace_id

每请求一个 RagTrace，贯穿 retriever/reranker 各步。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_rag_dir
from mokioclaw.core.utils import utc_now

logger = get_logger(__name__)


def _new_trace_id() -> str:
    """生成唯一 trace_id（复用 reliability/trace 风格）"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"rag-trace-{stamp}-{uuid4().hex[:6]}"


@dataclass
class RagTrace:
    """单请求的全链路 trace 记录

    用法：
        trace = new_trace()
        trace.record("query_rewrite", queries=["q1","q2"], ms=12)
        ...
        trace.save()
    """

    trace_id: str = field(default_factory=_new_trace_id)
    query: str = ""
    steps: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    degraded: list[str] = field(default_factory=list)
    _start_ms: dict[str, float] = field(default_factory=dict)

    def record(self, step: str, **fields: object) -> None:
        """记录一步（如 vector_recall / bm25_recall / rrf_fuse / rerank / fetch_parent）"""
        entry = {"step": step, "ts": utc_now()}
        entry.update(fields)
        self.steps.append(entry)
        if fields.get("degraded"):
            self.degraded.append(step)

    def mark_degraded(self, reason: str) -> None:
        """标记降级（如 reranker unavailable）"""
        self.degraded.append(reason)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "query": self.query,
            "created_at": self.created_at,
            "steps": self.steps,
            "degraded": self.degraded,
        }

    def save(self, traces_dir: Path | None = None) -> Path | None:
        """落盘到 .mokioclaw/rag/traces/{trace_id}.jsonl

        失败不抛（trace 不能影响主流程）。
        """
        import json

        path = (traces_dir or (default_rag_dir() / "traces"))
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{self.trace_id}.jsonl"
        try:
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self.to_dict(), ensure_ascii=False, default=str))
                f.write("\n")
            return target
        except Exception as exc:  # noqa: BLE001
            logger.debug("rag trace save failed: %s", exc)
            return None


def new_trace(query: str = "") -> RagTrace:
    """创建一个新 trace（绑定 query）"""
    t = RagTrace()
    t.query = query
    return t
