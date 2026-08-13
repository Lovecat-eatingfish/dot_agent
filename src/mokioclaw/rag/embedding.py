"""Embedding 抽象 + 双后端（OpenAI 远程 / 本地 sentence-transformers）

读 RAG_EMBEDDING_BACKEND env 切换后端：
- openai（默认）：复用 openai_provider._validate_env() 拿 api_key/base_url，远程 text-embedding
- local：本地 sentence-transformers（离线，但需下载模型；首次加载慢、体积大）

统一暴露 embed_texts / embed_query，屏蔽后端差异。
"""
from __future__ import annotations

import os
from typing import Any, Protocol

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class Embedder(Protocol):
    """Embedder 统一接口"""
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def create_embedder() -> Embedder:
    """工厂：按 RAG_EMBEDDING_BACKEND env 创建 embedder 实例

    - openai（默认）：langchain_openai.OpenAIEmbeddings
    - local：langchain_huggingface.HuggingFaceEmbeddings（需额外装 sentence-transformers）
    """
    backend = os.getenv("RAG_EMBEDDING_BACKEND", "openai").strip().lower()
    if backend == "local":
        return _create_local_embedder()
    return _create_openai_embedder()


def _create_openai_embedder() -> Embedder:
    from langchain_openai import OpenAIEmbeddings

    from mokioclaw.providers.openai_provider import _validate_env

    env = _validate_env()
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    return OpenAIEmbeddings(
        api_key=env["api_key"],
        base_url=env["base_url"],
        model=model,
    )


def _create_local_embedder() -> Embedder:
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError as exc:
        raise RuntimeError(
            "local embedding backend requires 'langchain-huggingface' and "
            "'sentence-transformers'. Install them or set RAG_EMBEDDING_BACKEND=openai."
        ) from exc

    model = os.getenv("RAG_LOCAL_MODEL", "BAAI/bge-small-zh-v1.5")
    logger.info("loading local embedding model: %s (first load may be slow)", model)
    return HuggingFaceEmbeddings(model_name=model)


class FakeEmbedder:
    """确定性 fake embedder（测试用，不依赖网络/模型）

    用简单哈希把文本映射到固定维度向量，保证相同文本得相同向量，
    使 query 能命中语义相近的 chunk（基于词重叠的近似）。
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        """基于词频的 bag-of-words 式哈希向量（确定性）"""
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = hash(token) & 0x7FFFFFFF
            vec[h % self.dim] += 1.0
        # L2 归一化
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
