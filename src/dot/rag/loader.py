"""
dot.rag.loader — 文档加载与分块

对应 RAG 全链路中的「数据准备」环节：
  - 支持 .txt / .md / .pdf / .docx（依赖对应 loader 包，缺什么装什么）
  - 使用 RecursiveCharacterTextSplitter 按分隔符层级递归切分，
    中文场景下段落/句子边界保留得比固定长度切分更好
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import RagSettings

# 后缀 -> loader 类路径（懒加载，避免装一堆不用的依赖）
_LOADERS: dict[str, str] = {
    ".txt": "langchain_community.document_loaders.TextLoader",
    ".md": "langchain_community.document_loaders.UnstructuredMarkdownLoader",
    ".pdf": "langchain_community.document_loaders.PyPDFLoader",
    ".docx": "langchain_community.document_loaders.Docx2txtLoader",
}


def _load_one(path: Path) -> list[Document]:
    module_path, class_name = _LOADERS[path.suffix.lower()].rsplit(".", 1)
    import importlib

    loader_cls = getattr(importlib.import_module(module_path), class_name)
    return loader_cls(str(path)).load()


def load_file(path: str | Path) -> str:
    """加载单个文件，返回纯文本"""
    path = Path(path)
    if path.suffix.lower() not in _LOADERS:
        raise ValueError(f"不支持的文件格式: {path.suffix}")
    docs = _load_one(path)
    return "\n\n".join(d.page_content for d in docs)


def load_documents(docs_dir: str | Path, settings: RagSettings | None = None) -> list[Document]:
    """加载目录下所有支持格式的文档"""
    settings = settings or RagSettings()
    docs_dir = Path(docs_dir)
    if not docs_dir.is_dir():
        raise FileNotFoundError(f"文档目录不存在: {docs_dir}")

    documents: list[Document] = []
    for path in sorted(docs_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _LOADERS:
            continue
        try:
            documents.extend(_load_one(path))
        except Exception as exc:  # 单个文件失败不阻断整个目录
            import logging

            logging.getLogger(__name__).warning("加载 %s 失败: %s", path, exc)

    for doc in documents:  # 记录来源，便于溯源
        doc.metadata.setdefault("source", str(doc.metadata.get("source", "")))
    return documents


def split_documents(
    documents: list[Document], settings: RagSettings | None = None
) -> list[Document]:
    """把长文档切成 chunk"""
    settings = settings or RagSettings()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
    )
    return splitter.split_documents(documents)


def load_and_split(docs_dir: str | Path, settings: RagSettings | None = None) -> list[Document]:
    return split_documents(load_documents(docs_dir, settings), settings)


def split_parent_child(
    text: str,
    parent_size: int = 1200,
    parent_overlap: int = 100,
    child_size: int = 500,
    child_overlap: int = 50,
    source: str = "",
) -> list[dict]:
    """父子分块（纯函数，不入库）

    子块用于向量检索（小而准），命中后回带父块作上下文（大而全）。
    返回 [{"parent_id", "parent_text", "children": ["...", ...]}, ...]
    """
    separators = ["\n\n", "\n", "。", "！", "？", "，", " ", ""]

    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=parent_size, chunk_overlap=parent_overlap, separators=separators
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=child_size, chunk_overlap=child_overlap, separators=separators
    )

    result: list[dict] = []
    for i, parent_text in enumerate(parent_splitter.split_text(text)):
        children = child_splitter.split_text(parent_text)
        result.append({
            "parent_id": f"p{i}",
            "parent_text": parent_text,
            "children": children,
            "source": source,
        })
    return result


def split_parent_child_documents(
    documents: list[Document], settings: RagSettings | None = None
) -> list[Document]:
    """把文档切成父子结构，返回子块 Document 列表

    每个子块 metadata 携带 doc 侧信息 + parent_id + parent_text，
    检索命中子块后可直接回带父块，无需二次查询。
    """
    settings = settings or RagSettings()
    chunks: list[Document] = []
    for doc in documents:
        families = split_parent_child(
            doc.page_content,
            parent_size=settings.parent_chunk_size,
            parent_overlap=settings.parent_chunk_overlap,
            child_size=settings.chunk_size,
            child_overlap=settings.chunk_overlap,
            source=str(doc.metadata.get("source", "")),
        )
        for fi, family in enumerate(families):
            for j, child in enumerate(family["children"]):
                chunks.append(Document(
                    page_content=child,
                    metadata={
                        "source": family["source"],
                        "parent_id": family["parent_id"],
                        "parent_index": fi,
                        "parent_text": family["parent_text"],
                        "chunk_index": j,
                    },
                ))
    return chunks
