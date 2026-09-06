"""
dot.rag.embeddings — 本地 Embedding 模型封装

使用 langchain-huggingface 加载本地 HuggingFace 模型（默认 BGE 中文小模型），
模型文件首次运行时自动下载到本地缓存，之后离线可用。

BGE 系列做检索时要求在查询侧加指令前缀（文档侧不加），这里用
InstructionAwareEmbeddings 显式实现，任何 embedding 模型通用。

后续迁移：换 embedding_model 配置即可（如接云端 embedding 服务，
可在此文件再实现一个 OpenAI 兼容的 Embeddings 适配，接口不变）。
"""
from __future__ import annotations

import os

# 国内网络默认走 hf-mirror 镜像下载模型（可用环境变量覆盖）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings

from .config import RagSettings

_EMBEDDINGS_CACHE: dict[tuple, Embeddings] = {}


class InstructionAwareEmbeddings(Embeddings):
    """包装 Embedding：查询侧加指令前缀，文档侧原样"""

    def __init__(self, inner: Embeddings, query_instruction: str = ""):
        self.inner = inner
        self.query_instruction = query_instruction

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if self.query_instruction:
            text = f"{self.query_instruction}{text}"
        return self.inner.embed_query(text)


def get_embeddings(settings: RagSettings | None = None) -> Embeddings:
    """获取本地 Embedding 实例（进程内缓存，避免重复加载模型）"""
    settings = settings or RagSettings()
    key = (settings.embedding_model, settings.embedding_device, settings.embedding_query_instruction)
    if key not in _EMBEDDINGS_CACHE:
        _EMBEDDINGS_CACHE[key] = InstructionAwareEmbeddings(
            HuggingFaceEmbeddings(
                model_name=settings.embedding_model,
                model_kwargs={"device": settings.embedding_device},
                encode_kwargs={"normalize_embeddings": True},
            ),
            query_instruction=settings.embedding_query_instruction,
        )
    return _EMBEDDINGS_CACHE[key]
