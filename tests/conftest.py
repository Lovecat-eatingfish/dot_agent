"""pytest 全局夹具：隔离环境变量，避免本地 .env 泄漏进测试进程"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_rag_token(monkeypatch: pytest.MonkeyPatch):
    """RAG 服务的 API Token 是可选配置，本地 .env 里的 RAG_API_TOKEN 不应影响测试"""
    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    yield
