"""引用溯源：从答案文本提取引用标记 + 校验引用合法性

生产级 RAG 必须可溯源——答案里的每个事实都要能追到上下文片段。
- 答案里标注 [1][2] 对应 context_builder 的 ContextCitation.index
- 校验：引用的编号必须在 citations 范围内（防 LLM 编造引用）
- 输出：引用列表（doc_id + source），供前端展示「来源」

不依赖 LLM，纯规则解析。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mokioclaw.rag.context_builder import ContextCitation

# 匹配 [1] [1,2] [1][2] [1-3] 等引用标记
_CITATION_RE = re.compile(r"\[(\d+(?:\s*[-,，]\s*\d+)*)\]")


@dataclass
class CitationRef:
    """一个引用（指向上下文中的某个片段）"""
    index: int  # 1-based
    doc_id: str
    source: str
    heading_path: str = ""


def extract_citations(answer: str) -> list[int]:
    """从答案文本提取所有引用编号（去重保序）

    支持 [1] [2] [1,2] [1-3] 等格式。
    """
    indices: list[int] = []
    seen: set[int] = set()
    for m in _CITATION_RE.finditer(answer):
        raw = m.group(1)
        for n in _parse_range(raw):
            if n not in seen and n > 0:
                seen.add(n)
                indices.append(n)
    return indices


def build_citation_refs(
    answer: str,
    citations: list[ContextCitation],
) -> list[CitationRef]:
    """构建引用列表（校验合法性 + 映射到 source）

    非法引用编号（超出 citations 范围）会被丢弃——防 LLM 编造引用。
    """
    by_index = {c.index: c for c in citations}
    refs: list[CitationRef] = []
    seen: set[int] = set()
    for n in extract_citations(answer):
        if n in seen:
            continue
        c = by_index.get(n)
        if c is None:
            continue  # 非法引用丢弃
        seen.add(n)
        refs.append(CitationRef(
            index=n,
            doc_id=c.doc_id,
            source=c.source,
            heading_path=c.heading_path,
        ))
    return refs


def _parse_range(raw: str) -> list[int]:
    """解析 '1' / '1,2' / '1-3' / '1，2' 为编号列表"""
    raw = raw.replace("，", ",").replace(" ", "")
    if "-" in raw:
        parts = raw.split("-")
        if len(parts) == 2:
            try:
                start, end = int(parts[0]), int(parts[1])
                if start <= end:
                    return list(range(start, end + 1))
            except ValueError:
                pass
        return []
    result: list[int] = []
    for part in raw.split(","):
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result
