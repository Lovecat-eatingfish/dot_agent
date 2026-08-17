"""文档解析：file / text / url → list[(text, page_or_none)]

按扩展名分发解析：
- .md / .txt：纯文本（page=None）
- .pdf：pypdf 逐页提取（page=页码）
- url：urllib 抓取 + BeautifulSoup 正文提取（去 script/style）
"""
from __future__ import annotations

import html
import urllib.error
import urllib.request
from pathlib import Path

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 解析结果：(文本, 页码或 None)
ParsedPage = tuple[str, int | None]

# URL 抓取默认超时
_DEFAULT_URL_TIMEOUT = 30.0


def load_text(content: str) -> list[ParsedPage]:
    """直接文本 → 单页"""
    return [(content, None)]


def load_file(path: Path) -> list[ParsedPage]:
    """按扩展名解析本地文件

    .md/.txt → 纯文本单页
    .pdf → 逐页文本（page=1-based 页码）
    其他扩展名 → 当纯文本读
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path)
    # md/txt/其他都按 utf-8 文本读（兼容 gbk）
    text = _read_text_file(path)
    return [(text, None)]


def load_url(url: str, timeout: float = _DEFAULT_URL_TIMEOUT) -> list[ParsedPage]:
    """抓取 URL 网页正文

    urllib 抓取 + BeautifulSoup 去 script/style 后提取正文文本。
    失败返回空列表并记日志（fail-soft，不抛）。
    安全：仅允许 http/https，拒绝 file://、私网/loopback/metadata（防 SSRF）；
    重定向目标逐跳校验；连接完成后复验 DNS 解析（防重绑定 TOCTOU——
    预检时解析到公网 IP、实际连接时被 rebind 到内网的情况会在复验被抓住并丢弃响应）。
    """
    if not url or not url.strip():
        return []
    try:
        from mokioclaw.rag.security import validate_fetch_url

        safe_url = validate_fetch_url(url)
    except ValueError as exc:
        logger.warning("rag load_url blocked (%s): %s", url, exc)
        return []

    class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
        """重定向逐跳校验：目标 URL 必须再次通过 SSRF 校验才允许跟随"""

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            try:
                validate_fetch_url(newurl)
            except ValueError as exc:
                raise urllib.error.URLError(f"redirect to blocked url: {newurl}: {exc}") from exc
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    req = urllib.request.Request(
        safe_url[0],
        headers={"User-Agent": "mokioclaw-rag/1.0"},
    )
    opener = urllib.request.build_opener(_ValidatingRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — scheme 已白名单
            raw = resp.read()
            final_url = resp.geturl()
            # 探测编码（必须在 with 内读 headers）
            charset = "utf-8"
            ctype = resp.headers.get("Content-Type", "")
            if "charset=" in ctype.lower():
                charset = ctype.lower().split("charset=")[-1].split(";")[0].strip() or charset
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning("rag load_url failed (%s): %s", safe_url, exc)
        return []

    # 连接后复验（防 DNS 重绑定）：重新解析原始与最终 host，
    # 任一解析结果落入私网/保留段 → 判定 rebind，丢弃已读取的响应体
    try:
        validate_fetch_url(safe_url[0])
        if final_url != safe_url[0]:
            validate_fetch_url(final_url)
    except ValueError as exc:
        logger.warning("rag load_url: DNS rebind suspected for %s (%s) — response discarded", url, exc)
        return []

    try:
        text = raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")

    return [(_extract_html_text(text), None)]


def _load_pdf(path: Path) -> list[ParsedPage]:
    """用 pypdf 逐页提取文本（page=1-based）"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pdf parsing requires 'pypdf'") from exc

    pages: list[ParsedPage] = []
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("pdf page %d extract failed: %s", i, exc)
            text = ""
        if text.strip():
            pages.append((text, i))
    return pages


def _read_text_file(path: Path) -> str:
    """读文本文件，utf-8 优先，回退 gbk"""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _extract_html_text(html_text: str) -> str:
    """HTML → 纯文本：去 script/style/head，保留正文"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # 无 bs4 时退化到正则去标签
        import re
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return html.unescape(text)

    soup = BeautifulSoup(html_text, "html.parser")
    # 删除非正文标签
    for tag in soup(["script", "style", "head", "noscript", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # 压缩多余空白
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
