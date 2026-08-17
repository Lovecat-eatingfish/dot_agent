"""Embedding 抽象 + 双后端（OpenAI 远程 / 本地 sentence-transformers）

读 RAG_EMBEDDING_BACKEND env 切换后端：
- openai（默认）：复用 openai_provider._validate_env() 拿 api_key/base_url，远程 text-embedding
- local：本地 sentence-transformers（离线，但需下载模型；首次加载慢、体积大）

统一暴露 embed_texts / embed_query，屏蔽后端差异。
"""
from __future__ import annotations

import os
from typing import Protocol

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class Embedder(Protocol):
    """Embedder 统一接口"""
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class OpenAIEmbedder:
    """OpenAI embedding 适配器"""

    def __init__(self, api_key: str, base_url: str | None = None, model: str = "text-embedding-3-small"):
        from langchain_openai import OpenAIEmbeddings
        self._embedder = OpenAIEmbeddings(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding"""
        return self._embedder.embed_documents(texts)  # langchain 方法名

    def embed_query(self, text: str) -> list[float]:
        """单个 query embedding"""
        return self._embedder.embed_query(text)


class LocalEmbedder:
    """本地 sentence-transformers 适配器"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:
            raise RuntimeError(
                "local embedding backend requires 'langchain-huggingface' and "
                "'sentence-transformers'. Install them or set RAG_EMBEDDING_BACKEND=openai."
            ) from exc

        logger.info("loading local embedding model: %s (first load may be slow)", model_name)
        self._embedder = HuggingFaceEmbeddings(model_name=model_name)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding"""
        return self._embedder.embed_documents(texts)  # langchain 方法名

    def embed_query(self, text: str) -> list[float]:
        """单个 query embedding"""
        return self._embedder.embed_query(text)


def create_embedder() -> Embedder:
    """工厂：按 RAG_EMBEDDING_BACKEND env 创建 embedder 实例

    - openai（默认）：langchain_openai.OpenAIEmbeddings
    - local：langchain_huggingface.HuggingFaceEmbeddings（需额外装 sentence-transformers）
    """
    import dotenv
    dotenv.load_dotenv()

    backend = os.getenv("RAG_EMBEDDING_BACKEND", "openai").strip().lower()

    if backend == "local":
        model = os.getenv("RAG_LOCAL_MODEL", "BAAI/bge-small-zh-v1.5")
        return LocalEmbedder(model_name=model)

    # openai backend：EMBEDDING_* 未配置时降级到主 API_KEY/MODEL/BASE_URL（validate_env 内置该逻辑）
    from mokioclaw.providers.openai_provider import validate_env
    env = validate_env()
    return OpenAIEmbedder(
        api_key=env["embedding_api_key"] or env["api_key"],
        base_url=env.get("embedding_base_url") or env.get("base_url"),
        model=env.get("embedding_model") or env.get("model") or "text-embedding-3-small",
    )


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
        """基于词频的 bag-of-words 式哈希向量（确定性）

        改进的哈希算法减少冲突，同时保持简单性。
        """
        vec = [0.0] * self.dim
        tokens = text.lower().split()

        # 改进的哈希算法：每个 token 使用多个哈希位置
        for token in tokens:
            # 主哈希位置
            h1 = hash(token) & 0x7FFFFFFF
            primary_idx = h1 % self.dim
            vec[primary_idx] += 1.0

            # 辅助哈希位置（减少冲突）
            h2 = hash(token + "_secondary") & 0x7FFFFFFF
            secondary_idx = h2 % self.dim
            vec[secondary_idx] += 0.5  # 辅助位置权重较低

        # L2 归一化
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
