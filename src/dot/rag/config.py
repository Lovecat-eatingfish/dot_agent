"""
dot.rag.config — RAG 模块配置（Pydantic Settings）

与 dot.ai.config 同风格：从项目根 .env / 环境变量加载，环境变量前缀 RAG_。
所有模型/向量库均先用本地方案，后续切换只需改配置：
  - Embedding：本地 HuggingFace 模型（默认 BAAI/bge-small-zh-v1.5）
  - 向量库：Milvus Lite（嵌入式本地文件，无需 Docker）
  - 生成：复用 dot.ai 的 OpenAI 兼容 Provider（本地部署可改 BASE_URL）
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CURR = Path(__file__).parent
PROJECT_ROOT = CURR.parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class RagSettings(BaseSettings):
    """RAG 配置

    支持从 .env / 环境变量加载（前缀 RAG_），例如：
      RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
      RAG_VECTOR_DB_URI=D:/data/milvus.db
      RAG_TOP_K=6
    """

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Embedding（本地 HuggingFace 模型，首次运行自动下载到 ~/.cache/huggingface）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "cpu"
    # BGE 系列检索时需在查询侧加指令前缀；换其他模型可置空
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："

    # 向量库：Chroma（本地持久化目录，零依赖开箱即用）
    # 后续迁移：LangChain 向量库接口统一，换成 Milvus 只需改 vectorstore.py
    vector_db_dir: str = str(PROJECT_ROOT / "data" / "rag" / "chroma")
    collection_name: str = "dot_rag"

    # 文档分块
    chunk_size: int = 500
    chunk_overlap: int = 50

    # 父子分块：子块用于向量检索（小而准），命中后回带父块作上下文（大而全）
    parent_chunk_size: int = 1200
    parent_chunk_overlap: int = 100
    enable_parent_child: bool = True

    # 存储后端选择：切换实现不改代码
    # 元数据（文档登记/trace）：sqlite（后续可扩展 mysql/postgres 等）
    metadata_backend: str = "sqlite"
    # 向量库：chroma（后续可扩展 milvus/faiss 等，走 LangChain 统一接口）
    vector_backend: str = "chroma"

    # 元数据库（SQLite）：文档登记 / trace 链路记录
    metadata_db_path: str = str(PROJECT_ROOT / "data" / "rag" / "rag.db")

    # Web 服务
    server_host: str = "127.0.0.1"
    server_port: int = 8000

    # 默认文档目录与检索参数（上传的文件也保存到这里）
    docs_dir: str = str(PROJECT_ROOT / "src" / "dot" / "rag" / "docs")
    top_k: int = 4

    # ---------- 检索增强（M1）----------
    # 混合检索：向量 + BM25 关键词，RRF/加权融合
    hybrid_enabled: bool = True
    bm25_enabled: bool = True
    fusion: str = "rrf"  # rrf / weighted

    # 重排序：cross_encoder（本地 bge-reranker）/ rrf / llm / none
    rerank: str = "cross_encoder"
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_top_n: int = 6

    # 上下文压缩：embedding 相似度低于阈值丢弃（0 = 关闭）
    compress_threshold: float = 0.0

    # 会话感知查询改写：多轮追问先改写为独立完整问句再检索
    query_rewrite: bool = True

    # 生成模型（留空则使用 dot.ai 的 OpenAISettings.model）
    generate_model: str = ""

    def resolve_generate_model(self) -> str:
        if self.generate_model:
            return self.generate_model
        from dot.ai.config import OpenAISettings

        return OpenAISettings().model
