"""上下文构建器：去重 + 长度截断 + Prompt 拼装

朴素 RAG 把所有召回片段原样拼进 prompt，问题：
1. 重复片段（多个 child 属于同一 parent）→ 上下文冗余
2. 片段总长超 LLM 上下文 → 截断丢信息或报错
3. 缺少结构 → LLM 难以区分片段边界

本模块解决：
- 去重：按 parent_id 去重（同 parent 只留一次）
- 长度预算：按 max_chars 截断，保留高相关片段
- 结构化：每片段带编号 + 溯源元数据，供引用溯源用
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mokioclaw.rag.types import ParentChunk


@dataclass
class ContextCitation:
    """上下文中一个可引用片段的元信息"""
    index: int  # 1-based，对应答案里的 [1][2]
    content: str
    doc_id: str
    source: str
    parent_id: str
    heading_path: str = ""


def build_context(
    parents: list[ParentChunk],
    *,
    max_chars: int = 6000,
) -> tuple[str, list[ContextCitation]]:
    """构建结构化上下文文本 + 引用列表

    Args:
        parents: 按相关度降序的父块（reorder 后）
        max_chars: 上下文总字符预算

    Returns:
        (context_text, citations)
        - context_text: 带编号的片段拼接文本
        - citations: 引用列表（index 与文本里的 [n] 对应）
    """
    if not parents:
        return "", []

    # 去重：同 parent_id 只留一次（理论上 retrieve 已去重，这里兜底）
    seen: set[str] = set()
    unique: list[ParentChunk] = []
    for p in parents:
        if p.parent_id in seen:
            continue
        seen.add(p.parent_id)
        unique.append(p)

    citations: list[ContextCitation] = []
    parts: list[str] = []
    used_chars = 0
    # 预留片段编号和换行的开销
    overhead_per_item = 20

    for i, p in enumerate(unique, start=1):
        content = p.content.strip()
        if not content:
            continue
        if used_chars + len(content) + overhead_per_item > max_chars and parts:
            # 预算用尽，停止追加（保证至少有一个片段）
            break
        meta = p.metadata or {}
        citation = ContextCitation(
            index=i,
            content=content,
            doc_id=p.doc_id,
            source=str(meta.get("source", "")),
            parent_id=p.parent_id,
            heading_path=str(meta.get("heading_path", "")),
        )
        citations.append(citation)
        # 结构化片段：[1] <heading_path>\n<content>
        header = f"[{i}]"
        if citation.heading_path:
            header += f" ({citation.heading_path})"
        parts.append(f"{header}\n{content}")
        used_chars += len(content) + overhead_per_item

    context_text = "\n\n---\n\n".join(parts)
    return context_text, citations
