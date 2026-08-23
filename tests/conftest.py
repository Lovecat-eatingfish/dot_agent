"""pytest 全局夹具：隔离环境变量，避免本地 .env 泄漏进测试进程"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保测试优先使用源码目录 src/dot，而不是 site-packages 里的残留旧副本
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolate_rag_token(monkeypatch: pytest.MonkeyPatch):
    """RAG 服务的 API Token 是可选配置，本地 .env 里的 RAG_API_TOKEN 不应影响测试"""
    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    yield
