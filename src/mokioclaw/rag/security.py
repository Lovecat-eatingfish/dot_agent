"""RAG 安全工具：URL SSRF 校验 + doc_id 路径净化"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

# doc_id 长度限制，可通过环境变量配置
_MAX_DOC_ID_LENGTH = int(os.getenv("MOKIO_MAX_DOC_ID_LENGTH", "200"))
# doc_id 允许字符（禁止路径分隔与穿越）
_DOC_ID_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._:@+-]{{0,{_MAX_DOC_ID_LENGTH}}}$")
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
})


def sanitize_doc_id(doc_id: str | None, *, fallback: str = "doc") -> str:
    """净化 doc_id，防止 parents/{doc_id}.json 路径穿越。

    - 拒绝空、含 / \\ .. 的值
    - 仅允许安全字符集；过长则截断
    - 不合法时退化为 hash 后的稳定 id
    """
    raw = (doc_id or "").strip()
    if not raw:
        raw = fallback
    # 显式拒绝路径成分
    if any(x in raw for x in ("/", "\\", "..")) or raw in {".", ".."}:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"safe_{digest}"
    if not _DOC_ID_RE.match(raw):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"safe_{digest}"
    # 使用配置的最大长度而不是硬编码
    return raw[:_MAX_DOC_ID_LENGTH]


def validate_fetch_url(url: str) -> tuple[str, list[str]]:
    """校验 URL 并返回规范化 URL 和所有解析的 IP 地址

    规则：
    - 仅 http/https
    - 拒绝 userinfo
    - host 解析后的所有 IP 不得为 loopback/link-local/private/reserved/unspecified
    - 拒绝已知 metadata 主机名
    - 返回解析的 IP 列表，供后续使用时验证，防止 DNS 重绑定攻击

    Returns:
        (规范化 URL, 解析的 IP 地址列表)

    Raises:
        ValueError: URL 不符合安全规则
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("url is empty")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported url scheme: {scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain userinfo")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("url missing host")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError(f"blocked host: {host}")

    resolved_ips = []

    # 字面量 IP
    try:
        ip = ipaddress.ip_address(host)
        _assert_public_ip(ip)
        resolved_ips.append(str(ip))
    except ValueError:
        # 主机名 → 解析 A/AAAA
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"dns resolve failed: {host}") from exc
        if not infos:
            raise ValueError(f"dns resolve empty: {host}")
        seen: set[str] = set()
        for info in infos:
            ip_str = info[4][0]
            if ip_str not in seen:
                seen.add(ip_str)
                resolved_ips.append(ip_str)
                try:
                    _assert_public_ip(ipaddress.ip_address(ip_str))
                except ValueError as exc:
                    raise ValueError(f"blocked resolved ip {ip_str} for host {host}") from exc

    # 还原无 fragment 的 URL（fragment 对抓取无意义）
    netloc = parsed.netloc
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    normalized_url = f"{scheme}://{netloc}{path}{query}"
    return normalized_url, resolved_ips


def _assert_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if any((
        ip.is_loopback,
        ip.is_link_local,
        ip.is_private,
        ip.is_reserved,
        ip.is_multicast,
        ip.is_unspecified,
    )):
        raise ValueError(f"non-public ip not allowed: {ip}")
    # 云 metadata 常见链路本地扩展
    if isinstance(ip, ipaddress.IPv4Address) and str(ip) == "169.254.169.254":
        raise ValueError("metadata ip not allowed")
