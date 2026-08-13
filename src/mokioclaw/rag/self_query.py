"""Self-Query：自然语言自动解析结构化过滤条件

用户问题常含隐式过滤（「只看 v2 的」「2024 年的文档」），
Self-Query 用 LLM 把这些提取成 metadata filter，检索时过滤。

减法设计：
- 只做确定性结构化字段（type/version/source 等已存在 metadata 的字段）
- 复杂自然语言时间/否定不做（交给 query 改写）
- LLM 不可用 → 降级返回空 filter（不过滤）
"""
from __future__ import annotations

import json
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 允许作为过滤条件的 metadata 字段白名单（防注入 + 防过滤不存在的字段）
_ALLOWED_FILTER_FIELDS = frozenset({
    "source", "type", "version", "doc_id", "page", "heading_path",
})

_llm: Any = None


def _get_llm() -> Any | None:
    global _llm
    if _llm is not None:
        return _llm
    try:
        from mokioclaw.providers.openai_provider import create_model
        _llm = create_model()
        return _llm
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag self_query: LLM unavailable, will degrade: %s", exc)
        return None


def parse_filter(query: str) -> dict[str, Any]:
    """从用户问题解析结构化过滤条件

    返回 metadata filter dict（如 {"source": "api.md", "version": 2}）。
    LLM 不可用或解析失败 → 返回 {} （不过滤）。
    """
    if not query.strip():
        return {}
    llm = _get_llm()
    if llm is None:
        return {}
    prompt = (
        f"Extract structured metadata filters from this question. "
        f"Allowed fields: {sorted(_ALLOWED_FILTER_FIELDS)}. "
        f"Only include fields explicitly mentioned. "
        f"Output ONLY a JSON object (empty {{}} if none), no explanation.\n\n"
        f"Question: {query}"
    )
    try:
        from langchain_core.messages import HumanMessage
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = _extract_text(resp)
        parsed = _parse_json_object(text)
        # 白名单过滤 + 类型清洗
        cleaned: dict[str, Any] = {}
        for k, v in parsed.items():
            if k in _ALLOWED_FILTER_FIELDS and v is not None:
                # version 转 int
                if k == "version":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                cleaned[k] = v
        return cleaned
    except Exception as exc:  # noqa: BLE001
        logger.debug("rag self_query failed, degraded: %s", exc)
        return {}


def _extract_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    content = getattr(resp, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def _parse_json_object(text: str) -> dict[str, Any]:
    """从文本提取 JSON 对象（容错 markdown 围栏）"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(cleaned[start:end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
