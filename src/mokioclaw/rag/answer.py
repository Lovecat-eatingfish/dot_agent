"""答案生成：LLM 基于上下文生成带引用的答案（复用项目 create_model）

对齐生产级 RAG 的「生成阶段」：
- 强制 Prompt 约束：仅基于上下文回答、禁止幻觉、带 [n] 引用
- 上下文用 context_builder 结构化拼装（带编号）
- 答案经 citation.extract_citations 校验引用合法性

降级：LLM 不可用 → 返回「上下文片段列表」作为答案（检索结果直出），
不阻塞用户拿到信息。
"""
from __future__ import annotations

from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.rag.context_builder import ContextCitation, build_context
from mokioclaw.rag.types import ParentChunk

logger = get_logger(__name__)

# 模块级 LLM 单例
_llm: Any = None

_ANSWER_PROMPT = """You are a precise question-answering assistant. Answer the question based ONLY on the context below.

Rules:
1. Use ONLY information present in the context. Do not use outside knowledge.
2. If the context does not contain the answer, say "I cannot answer this based on the provided context."
3. Cite sources using [n] notation matching the context fragment numbers (e.g. [1], [2]).
4. Be concise and factual.

Context:
{context}

Question: {question}

Answer:"""


def _get_llm() -> Any | None:
    """获取 LLM（复用项目 create_model），失败返回 None 触发降级"""
    global _llm
    if _llm is not None:
        return _llm
    try:
        from mokioclaw.providers.openai_provider import create_model
        _llm = create_model()
        return _llm
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag answer: LLM unavailable, will degrade: %s", exc)
        return None


def generate_answer(
    question: str,
    parents: list[ParentChunk],
    *,
    max_context_chars: int = 6000,
) -> tuple[str, list[ContextCitation]]:
    """生成带引用的答案

    Returns:
        (answer, citations)
        - answer: LLM 生成的答案（含 [n] 引用）；LLM 不可用时返回片段拼接
        - citations: 上下文引用列表（index 与答案里的 [n] 对应）
    """
    context_text, citations = build_context(parents, max_chars=max_context_chars)
    if not context_text or not citations:
        return "I cannot answer this based on the provided context.", []

    llm = _get_llm()
    if llm is None:
        # 降级：直出片段作为答案
        degraded = "\n\n".join(
            f"[{c.index}] {c.content[:200]}" for c in citations
        )
        return degraded, citations

    prompt = _ANSWER_PROMPT.format(context=context_text, question=question)
    try:
        from langchain_core.messages import HumanMessage
        resp = llm.invoke([HumanMessage(content=prompt)])
        answer = _extract_text(resp)
        return answer, citations
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag answer generation failed, degraded: %s", exc)
        degraded = "\n\n".join(
            f"[{c.index}] {c.content[:200]}" for c in citations
        )
        return degraded, citations


def _extract_text(resp: Any) -> str:
    """从 LLM 响应提取文本（兼容 AIMessage / str）"""
    if isinstance(resp, str):
        return resp
    content = getattr(resp, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)
