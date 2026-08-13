"""Query 改写（对齐高级 RAG）

朴素 RAG 直接用原 query 检索，召回差。高级 RAG 在检索前改写 query：
1. Multi-Query：LLM 把一个问句扩写成多个角度的问句，多路检索后合并（覆盖更广）
2. HyDE：LLM 先生成一个假设性答案，用该答案的向量去检索（缩小 query-doc 语义鸿沟）
3. Step-Back：把具体问题抽象成更上位的问题（如「2022 年法国 GDP」→「法国经济」）

设计原则（减法）：
- LLM 不可用时全部降级为原 query 单元素列表
- 每个改写策略独立可选，组合用
- 复用项目 create_model()，不额外配模型
"""
from __future__ import annotations

from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 模块级 LLM 单例（首次用才创建，避免 import 时副作用）
_llm: Any = None


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
        logger.debug("rag query_transform: LLM unavailable, will degrade: %s", exc)
        return None


def rewrite_multi_query(query: str, n: int = 3) -> list[str]:
    """Multi-Query：LLM 把 query 扩写成 n 个不同角度的问句

    降级：LLM 不可用或解析失败 → 返回 [query]
    """
    if not query.strip():
        return []
    llm = _get_llm()
    if llm is None:
        return [query]
    prompt = (
        f"You are an AI assistant. Rewrite the following question into {n} "
        f"different questions that capture the same intent from different angles. "
        f"Output ONLY a JSON list of strings, no explanation.\n\n"
        f"Question: {query}"
    )
    try:
        from langchain_core.messages import HumanMessage
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = _extract_text(resp)
        import json
        # 尝试从响应里提取 JSON 数组
        queries = _parse_json_list(text)
        if queries:
            # 始终保留原 query 作为兜底
            return [query] + [q for q in queries if q and q != query]
        return [query]
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag multi_query failed, degraded: %s", exc)
        return [query]


def rewrite_hyde(query: str) -> str:
    """HyDE：LLM 生成假设性答案，用其检索（缩小 query-doc 语义鸿沟）

    返回假设答案文本（用于向量化检索）。
    降级：LLM 不可用 → 返回原 query
    """
    if not query.strip():
        return query
    llm = _get_llm()
    if llm is None:
        return query
    prompt = (
        f"Generate a short hypothetical answer (2-3 sentences) as if you knew the "
        f"answer to this question. This will be used for retrieval, so write factual-"
        f"style text, not questions. No preamble.\n\n"
        f"Question: {query}"
    )
    try:
        from langchain_core.messages import HumanMessage
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = _extract_text(resp)
        return text.strip() or query
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag HyDE failed, degraded: %s", exc)
        return query


def rewrite_step_back(query: str) -> str:
    """Step-Back：把具体问题抽象成更上位的概念问题

    降级：LLM 不可用 → 返回原 query
    """
    if not query.strip():
        return query
    llm = _get_llm()
    if llm is None:
        return query
    prompt = (
        f"Rewrite the following question into a more general, higher-level "
        f"question about the underlying concept. Output ONLY the rewritten "
        f"question, nothing else.\n\n"
        f"Question: {query}"
    )
    try:
        from langchain_core.messages import HumanMessage
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = _extract_text(resp)
        return text.strip() or query
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag step_back failed, degraded: %s", exc)
        return query


def transform_query(
    query: str,
    *,
    multi_query: bool = False,
    hyde: bool = False,
    step_back: bool = False,
) -> list[str]:
    """统一入口：按策略组合改写 query，返回检索用 query 列表

    - multi_query=True：返回多个改写问句
    - hyde=True：额外加 HyDE 假设答案
    - step_back=True：额外加 Step-Back 抽象问句
    全部关闭或降级 → [query]
    """
    queries: list[str] = []
    if multi_query:
        queries.extend(rewrite_multi_query(query))
    if hyde:
        hyde_text = rewrite_hyde(query)
        if hyde_text and hyde_text != query:
            queries.append(hyde_text)
    if step_back:
        sb = rewrite_step_back(query)
        if sb and sb != query:
            queries.append(sb)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries or [query]:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique or [query]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _extract_text(resp: Any) -> str:
    """从 LLM 响应提取文本（兼容 AIMessage / str）"""
    if isinstance(resp, str):
        return resp
    content = getattr(resp, "content", "")
    if isinstance(content, str):
        return content
    # 某些响应 content 是 list[dict]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _parse_json_list(text: str) -> list[str]:
    """从文本里提取 JSON 字符串数组（容错：前后可能有 markdown 围栏/解释）"""
    import json

    # 去掉可能的 markdown 围栏
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # 去首尾围栏行
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)
    # 找第一个 [ 到最后一个 ]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(cleaned[start:end + 1])
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except Exception:  # noqa: BLE001
        pass
    return []
