"""WebSearchTool — 统一搜索工具壳

默认走 HTTP 公网搜索（DuckDuckGo HTML，自己发请求，无 API key、不依赖 Tavily）。
通过 SEARCH_BACKEND env 切换：web|local|code|rag|tavily。

对外契约保持不变：{ok, query, answer, results[{title,url,content,score}]}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.utils import WebSearchResult, coerce_bool
from mokioclaw.tools.search_backend import SearchBackend, create_search_backend


def web_search(
    query: str,
    max_results: int | str = 5,
    include_answer: bool | str = True,
    *,
    backend: SearchBackend | None = None,
    workspace: Path | None = None,
) -> WebSearchResult:
    """搜索公网或本地资源。

    Args:
        query: 搜索词
        max_results: 最大结果数 1-10
        include_answer: 是否生成简要 answer 摘要
        backend: 可选注入后端（测试用）
        workspace: 本地 code/rag 搜索的工作区根
    """
    try:
        max_value = int(max_results)
    except (TypeError, ValueError):
        max_value = 5
    max_value = max(1, min(max_value, 10))
    answer_value = coerce_bool(include_answer, default=True)

    b = backend or create_search_backend(workspace)
    try:
        result = b.search(query, max_results=max_value, include_answer=answer_value)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "query": query,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(result, dict):
        return {"ok": False, "query": query, "error": "backend returned non-dict"}
    result.setdefault("query", query)
    result.setdefault("results", [])
    result.setdefault("answer", "")
    if "ok" not in result:
        result["ok"] = True
    return result  # type: ignore[return-value]


def build_web_search_tool(workspace: Path | None = None) -> StructuredTool:
    """构建 WebSearchTool。"""

    def _search(
        query: str,
        max_results: int | str = 5,
        include_answer: bool | str = True,
    ) -> dict[str, Any]:
        return web_search(
            query,
            max_results=max_results,
            include_answer=include_answer,
            workspace=workspace,
        )

    return StructuredTool.from_function(
        name="WebSearchTool",
        func=_search,
        description=(
            "Search the web via direct HTTP (DuckDuckGo HTML by default, no API key). "
            "Set SEARCH_BACKEND=local|code|rag for workspace/RAG-only, or tavily for Tavily API. "
            "Args: query, optional max_results, optional include_answer. "
            "Returns answer and result sources with title, url, content, and score."
        ),
    )
