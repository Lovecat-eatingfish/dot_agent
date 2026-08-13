"""搜索后端抽象 — WebSearchTool 的可替换实现

设计（对齐 StoreBackend / CacheBackend）：
- SearchBackend Protocol：只暴露 search()，返回统一 WebSearchResult 结构
- 默认 web：自己发 HTTP 请求搜公网（DuckDuckGo HTML，无 API key、无 Tavily）
- local/code/rag：工作区/本地知识库
- tavily：可选三方（需 key）

SEARCH_BACKEND:
- web（默认）：HTTP 公网搜索（DuckDuckGo HTML）
- local：code + rag 混合
- code：仅工作区文本/文件名搜索
- rag：仅本地 RAG 向量库
- tavily：Tavily 公网搜索（需 key）
"""
from __future__ import annotations

import html as html_lib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import WebSearchResult

logger = get_logger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (compatible; MokioClawSearch/1.0; +https://github.com/mokioclaw)"
)
_HTTP_TIMEOUT = float(os.getenv("SEARCH_HTTP_TIMEOUT", "12"))


class SearchBackend(Protocol):
    """搜索后端契约（鸭子类型，无需继承）"""

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        ...


# ---------------------------------------------------------------------------
# 本地：工作区代码 / 文档搜索
# ---------------------------------------------------------------------------


class CodeSearchBackend:
    """工作区内关键词搜索（纯本地，无网络）

    策略：
    1. 文件名/路径命中（query token 出现在路径）
    2. 文件内容正则/子串命中（优先 ripgrep 路径由 grep 工具承担的逻辑简化版）
    结果归一化为 title/url/content/score，url 用 file:// 相对路径。
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "query must not be empty", "query": q}

        tokens = [t for t in re.split(r"\s+", q.lower()) if len(t) >= 2]
        if not tokens:
            tokens = [q.lower()]

        hits: list[dict[str, Any]] = []
        # 1) 路径命中
        for path in self._iter_text_files():
            rel = self._rel(path)
            path_l = rel.lower()
            path_score = sum(1 for t in tokens if t in path_l)
            if path_score <= 0:
                continue
            snippet = self._read_head(path, 800)
            hits.append({
                "title": path.name,
                "url": f"file://{rel}",
                "content": snippet,
                "score": float(path_score) + 0.5,
                "_path": str(path),
            })

        # 2) 内容命中（限制扫描文件数，避免大仓库卡死）
        content_hits = self._content_search(tokens, limit_files=400)
        hits.extend(content_hits)

        # 去重（同 path 保留高分）
        best: dict[str, dict[str, Any]] = {}
        for h in hits:
            key = h.get("_path") or h.get("url") or h.get("title")
            prev = best.get(str(key))
            if prev is None or float(h.get("score") or 0) > float(prev.get("score") or 0):
                best[str(key)] = h

        ranked = sorted(best.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
        results = []
        for h in ranked[:max_results]:
            results.append({
                "title": str(h.get("title", "")),
                "url": str(h.get("url", "")),
                "content": str(h.get("content", ""))[:1200],
                "score": h.get("score"),
            })

        answer = ""
        if include_answer and results:
            answer = self._summarize(q, results)

        return {
            "ok": True,
            "query": q,
            "answer": answer,
            "results": results,
            "backend": "code",
        }

    def _iter_text_files(self) -> list[Path]:
        skip = {".git", ".mokioclaw", ".venv", "__pycache__", ".pytest_cache",
                "node_modules", ".idea", "dist", "build", ".tox"}
        exts = {".py", ".md", ".txt", ".rst", ".toml", ".yml", ".yaml", ".json",
                ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".c", ".h",
                ".cpp", ".cs", ".sh", ".bash", ".zsh", ".css", ".html", ".sql"}
        out: list[Path] = []
        root = self.workspace
        if not root.exists():
            return out
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in skip for part in p.parts):
                continue
            if p.suffix.lower() not in exts and p.name not in {"Dockerfile", "Makefile", "README"}:
                continue
            # 跳过过大文件
            try:
                if p.stat().st_size > 512_000:
                    continue
            except OSError:
                continue
            out.append(p)
            if len(out) >= 2000:
                break
        return out

    def _content_search(self, tokens: list[str], *, limit_files: int) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        files = self._iter_text_files()[:limit_files]
        # 用第一个较长 token 做主模式，其余加权
        primary = max(tokens, key=len)
        try:
            pat = re.compile(re.escape(primary), re.IGNORECASE)
        except re.error:
            return hits

        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not pat.search(text):
                continue
            # 取匹配附近片段
            m = pat.search(text)
            start = max(0, (m.start() if m else 0) - 120)
            end = min(len(text), (m.end() if m else 0) + 400)
            snippet = text[start:end].replace("\n", " ").strip()
            score = 1.0 + sum(1 for t in tokens if t in text.lower()) * 0.3
            rel = self._rel(path)
            hits.append({
                "title": path.name,
                "url": f"file://{rel}",
                "content": snippet[:1200],
                "score": score,
                "_path": str(path),
            })
            if len(hits) >= 50:
                break
        return hits

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace)).replace("\\", "/")
        except Exception:
            return path.name

    @staticmethod
    def _read_head(path: Path, n: int) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:n]
        except OSError:
            return ""

    @staticmethod
    def _summarize(query: str, results: list[dict[str, Any]]) -> str:
        lines = [f"Local workspace search for: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title')} ({r.get('url')})")
            content = str(r.get("content", "")).strip()
            if content:
                lines.append(f"   {content[:200]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 本地：RAG 知识库搜索
# ---------------------------------------------------------------------------


class RagSearchBackend:
    """本地 RAG 混合检索（ChromaDB + BM25），无网络

    依赖 workspace 下已有 ingest 的 .mokioclaw/rag 数据。
    无数据 / 初始化失败 → 返回空结果（ok=True, results=[]），由上层融合。
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self._retriever = None
        self._init_error: str | None = None

    def _ensure(self) -> bool:
        if self._retriever is not None:
            return True
        if self._init_error is not None:
            return False
        try:
            from mokioclaw.rag.embedding import FakeEmbedder, create_embedder
            from mokioclaw.rag.backends.local_file import LocalFileBackend
            from mokioclaw.rag.retrieval import HybridRetriever
            from mokioclaw.core.paths import default_rag_dir

            persist = default_rag_dir(self.workspace) / "chroma"
            # 无 chroma 目录时不强行 create_embedder（可能缺 API key）
            if not persist.exists():
                self._init_error = "no local rag index"
                return False
            try:
                embedder = create_embedder()
            except Exception:
                embedder = FakeEmbedder()
            backend = LocalFileBackend(embedder=embedder, persist_dir=persist)
            self._retriever = HybridRetriever(backend=backend, embedder=embedder, top_k=5)
            return True
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            logger.debug("rag search backend init failed: %s", exc)
            return False

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "query must not be empty", "query": q}
        if not self._ensure() or self._retriever is None:
            return {
                "ok": True,
                "query": q,
                "answer": "",
                "results": [],
                "backend": "rag",
                "note": self._init_error or "rag unavailable",
            }
        try:
            self._retriever.top_k = max_results
            parents = self._retriever.retrieve(q)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "query": q,
                "error": f"rag search failed: {type(exc).__name__}: {exc}",
                "backend": "rag",
            }

        results = []
        for i, p in enumerate(parents[:max_results]):
            meta = p.metadata or {}
            title = str(meta.get("heading_path") or meta.get("source") or p.doc_id or f"chunk-{i}")
            results.append({
                "title": title,
                "url": f"rag://{p.doc_id}/{p.parent_id}",
                "content": p.content[:1200],
                "score": 1.0 - i * 0.05,
            })
        answer = ""
        if include_answer and results:
            answer = "Local RAG hits:\n" + "\n".join(
                f"- {r['title']}: {r['content'][:160]}" for r in results[:3]
            )
        return {
            "ok": True,
            "query": q,
            "answer": answer,
            "results": results,
            "backend": "rag",
        }


# ---------------------------------------------------------------------------
# 公网 Web：自己发 HTTP（DuckDuckGo HTML，无 API key）
# ---------------------------------------------------------------------------


class HttpWebSearchBackend:
    """用 stdlib urllib 直接请求公网搜索页，解析结果。

    默认走 DuckDuckGo HTML（https://html.duckduckgo.com/html/）：
    - 不需要 API key
    - 不依赖 tavily-python
    - 结果结构对齐 WebSearchResult

    说明：HTML 结构可能变化，解析失败时 ok=False 并带 error；
    测试可 monkeypatch _http_get / _parse_ddg_html。
    """

    DDG_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, *, timeout: float | None = None, user_agent: str | None = None) -> None:
        self.timeout = timeout if timeout is not None else _HTTP_TIMEOUT
        self.user_agent = user_agent or _DEFAULT_UA

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "query must not be empty", "query": q}

        max_value = max(1, min(int(max_results), 10))
        try:
            html = self._http_post_form(
                self.DDG_URL,
                {"q": q, "b": ""},
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "query": q,
                "error": f"http search failed: {type(exc).__name__}: {exc}",
                "backend": "web",
            }

        try:
            results = self._parse_ddg_html(html, max_results=max_value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ddg html parse failed: %s", exc)
            return {
                "ok": False,
                "query": q,
                "error": f"parse failed: {type(exc).__name__}: {exc}",
                "backend": "web",
            }

        answer = ""
        if include_answer and results:
            answer = self._summarize(q, results)

        return {
            "ok": True,
            "query": q,
            "answer": answer,
            "results": results,
            "backend": "web",
        }

    def _http_post_form(self, url: str, fields: dict[str, str]) -> str:
        """POST application/x-www-form-urlencoded，返回解码后的 HTML 文本。"""
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                charset = "utf-8"
                ctype = resp.headers.get("Content-Type", "")
                if "charset=" in ctype.lower():
                    charset = ctype.lower().split("charset=")[-1].split(";")[0].strip() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"URL error: {exc.reason}") from exc

    @staticmethod
    def _parse_ddg_html(html: str, *, max_results: int) -> list[dict[str, Any]]:
        """解析 DuckDuckGo HTML 结果页。

        主要结构：
        <a class="result__a" href="...">title</a>
        <a class="result__snippet">...</a> 或 <td class="result__snippet">
        """
        results: list[dict[str, Any]] = []
        # 结果块：class 含 result__a 的链接
        link_re = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        # snippet 在结果附近
        snippet_re = re.compile(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div|span)>',
            re.IGNORECASE | re.DOTALL,
        )

        # 按 result 区块切分更稳
        blocks = re.split(r'class="[^"]*result[^"]*"', html)
        # 第一个 split 前缀不是结果
        candidates = blocks[1:] if len(blocks) > 1 else [html]

        for block in candidates:
            # 补回被 split 吃掉的前缀，方便统一正则
            chunk = 'class="result" ' + block
            m = link_re.search(chunk)
            if not m:
                # 退化：整页扫
                continue
            href = html_lib.unescape(m.group(1).strip())
            title = _strip_tags(m.group(2))
            href = _unwrap_ddg_redirect(href)
            if not href or not title:
                continue
            if href.startswith("https://duckduckgo.com") and "/y.js" in href:
                continue

            sn_m = snippet_re.search(chunk)
            snippet = _strip_tags(sn_m.group(1)) if sn_m else ""
            results.append({
                "title": title[:300],
                "url": href[:2000],
                "content": snippet[:1200],
                "score": max(0.1, 1.0 - len(results) * 0.08),
            })
            if len(results) >= max_results:
                break

        # 区块解析失败时，整页扫 link
        if not results:
            for m in link_re.finditer(html):
                href = _unwrap_ddg_redirect(html_lib.unescape(m.group(1).strip()))
                title = _strip_tags(m.group(2))
                if not href or not title:
                    continue
                results.append({
                    "title": title[:300],
                    "url": href[:2000],
                    "content": "",
                    "score": max(0.1, 1.0 - len(results) * 0.08),
                })
                if len(results) >= max_results:
                    break

        return results

    @staticmethod
    def _summarize(query: str, results: list[dict[str, Any]]) -> str:
        lines = [f"Web search results for: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title')} — {r.get('url')}")
            content = str(r.get("content") or "").strip()
            if content:
                lines.append(f"   {content[:220]}")
        return "\n".join(lines)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo 结果链接常包一层 //duckduckgo.com/l/?uddg=<encoded>"""
    if not href:
        return href
    # 协议相对
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return urllib.parse.unquote(qs["uddg"][0])
    except Exception:  # noqa: BLE001
        pass
    return href


# ---------------------------------------------------------------------------
# Tavily（可选三方）
# ---------------------------------------------------------------------------


class TavilySearchBackend:
    """Tavily 公网搜索（需 TAVILY_API_KEY）"""

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return {"ok": False, "query": query, "error": "missing required .env setting: TAVILY_API_KEY"}
        try:
            from tavily import TavilyClient
        except ImportError as exc:
            return {"ok": False, "query": query, "error": f"tavily-python is not installed: {exc}"}

        max_value = max(1, min(int(max_results), 10))
        try:
            client = TavilyClient(api_key=api_key)
            response = client.search(
                query=query,
                search_depth="basic",
                max_results=max_value,
                include_answer=include_answer,
            )
        except Exception as exc:
            return {"ok": False, "query": query, "error": f"{type(exc).__name__}: {exc}"}

        results = []
        for item in response.get("results", []) or []:
            results.append({
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "content": str(item.get("content", ""))[:1200],
                "score": item.get("score"),
            })
        return {
            "ok": True,
            "query": query,
            "answer": response.get("answer") or "",
            "results": results,
            "backend": "tavily",
        }


# ---------------------------------------------------------------------------
# 本地混合：code + rag
# ---------------------------------------------------------------------------


class LocalCompositeBackend:
    """本地默认：代码搜索 + RAG，结果合并去重"""

    def __init__(self, workspace: Path | None = None) -> None:
        ws = (workspace or Path.cwd()).resolve()
        self.code = CodeSearchBackend(ws)
        self.rag = RagSearchBackend(ws)

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_answer: bool = True,
    ) -> WebSearchResult:
        code_res = self.code.search(query, max_results=max_results, include_answer=False)
        rag_res = self.rag.search(query, max_results=max_results, include_answer=False)

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        # 交错合并：code 优先（coding agent 场景），再 rag
        for src in (code_res.get("results") or [], rag_res.get("results") or []):
            if not isinstance(src, list):
                continue
            for item in src:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("url") or item.get("title") or "")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        results = merged[:max_results]
        backends = []
        if code_res.get("ok") and code_res.get("results"):
            backends.append("code")
        if rag_res.get("ok") and rag_res.get("results"):
            backends.append("rag")
        if not backends:
            # 两路都空也算成功（本地无命中），避免工具硬失败
            backends = ["local"]

        answer = ""
        if include_answer and results:
            answer = CodeSearchBackend._summarize(query, results)

        # 若两路都技术失败才 ok=False
        if not code_res.get("ok") and not rag_res.get("ok"):
            err = code_res.get("error") or rag_res.get("error") or "local search failed"
            return {"ok": False, "query": query, "error": str(err), "backend": "local"}

        return {
            "ok": True,
            "query": query,
            "answer": answer,
            "results": results,
            "backend": "+".join(backends),
        }


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_search_backend(workspace: Path | None = None) -> SearchBackend:
    """按 SEARCH_BACKEND env 创建后端

    - web（默认）：HTTP 公网搜索（DuckDuckGo HTML，无 key）
    - local：Code + RAG
    - code / rag / tavily：单一后端
    """
    name = os.getenv("SEARCH_BACKEND", "web").strip().lower()
    ws = workspace
    if name in {"web", "http", "ddg", "duckduckgo"}:
        return HttpWebSearchBackend()
    if name == "tavily":
        return TavilySearchBackend()
    if name == "code":
        return CodeSearchBackend(ws)
    if name == "rag":
        return RagSearchBackend(ws)
    if name == "local":
        return LocalCompositeBackend(ws)
    # 未知值 fallback web
    logger.debug("unknown SEARCH_BACKEND=%s, fallback web", name)
    return HttpWebSearchBackend()
