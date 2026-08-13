"""结构感知分割器（对齐 RAG 演进：朴素 → 高级）

朴素 RAG 用固定字符数切分，割裂语义。本模块实现高级 RAG 的结构感知分割：

1. 结构感知优先：
   - Markdown：按标题层级(`#`/`##`/`###`)切分，保留标题路径(heading_path)作元数据
   - 代码块：识别 ``` 围栏，代码块不跨切（避免腰斩函数）
   - 段落：按双换行切分，尽量在语义边界结束
2. 递归字符降级：结构切不开或单块仍超 chunk_size，按分隔符列表递归切，带 overlap
3. 元数据保留：每个 chunk 带 source/doc_id/chunk_index/heading_path/page/char_start/char_end

底层字符降级复用 LangChain 的 RecursiveCharacterTextSplitter（随 langchain 依赖可用）。

父子分块（对齐高级 RAG）：
- split_text_parent_child：parent 用结构感知切大块（完整上下文），
  child 对 parent 再递归字符切细块（向量化检索），child.metadata 带 parent_index。
  查询命中 child → 按 parent_id 取回 parent 原文，解决上下文残缺。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Markdown 标题正则：1-6 个 # 开头
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

# 代码围栏正则：``` 或 ~~~
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)

# 默认递归降级分隔符（中英文兼顾）
_DEFAULT_SEPARATORS = ["\n\n", "\n", "。", ".", "!", "?", " ", ""]


@dataclass
class Chunk:
    """分割后的一个文本块 + 溯源元数据"""
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def doc_id(self) -> str:
        return str(self.metadata.get("doc_id", ""))

    def to_document(self) -> Document:
        """转为 LangChain Document（供 langchain-chroma 直接消费）"""
        return Document(page_content=self.content, metadata=dict(self.metadata))


class StructureAwareSplitter:
    """结构感知分割器

    分两层：
    - 第一层 split_by_structure：按 Markdown 标题 + 代码块结构切，产出带 heading_path 的段落块
    - 第二层：对超 chunk_size 的段落块用 RecursiveCharacterTextSplitter 降级切（带 overlap）

    Args:
        chunk_size: 单块最大字符数（默认 1000）
        chunk_overlap: 降级字符切分时的重叠字符数（默认 200）
        separators: 降级切分的分隔符优先级列表
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
    ) -> None:
        self.chunk_size = max(1, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size - 1))
        self.separators = separators or list(_DEFAULT_SEPARATORS)
        # 字符降级层（延迟构造，参数可配）
        self._char_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            separators=self.separators,
            keep_separator=True,
        )

    def split_text(
        self,
        text: str,
        *,
        source: str = "",
        doc_id: str = "",
        page: int | None = None,
    ) -> list[Chunk]:
        """分割文本，产出带完整元数据的 Chunk 列表

        Args:
            text: 原始文本
            source: 来源标识（文件路径 / URL / "text"）
            doc_id: 文档唯一 id（用于删除/去重）
            page: 页码（PDF 解析时传入）
        """
        if not text or not text.strip():
            return []

        # 第一层：按结构切成段落块（带 heading_path）
        blocks = self._split_by_structure(text)

        chunks: list[Chunk] = []
        chunk_index = 0
        char_cursor = 0  # 在原文中的大致起始偏移（按 block 累计，近似）
        for block_content, heading_path, block_offset in blocks:
            # 第二层：超 size 的块降级字符切
            if len(block_content) > self.chunk_size:
                pieces = self._char_splitter.split_text(block_content)
            else:
                pieces = [block_content] if block_content.strip() else []

            for piece in pieces:
                piece = piece.strip()
                if not piece:
                    continue
                chunks.append(Chunk(
                    content=piece,
                    metadata={
                        "source": source,
                        "doc_id": doc_id,
                        "chunk_index": chunk_index,
                        "heading_path": " / ".join(heading_path) if heading_path else "",
                        "page": page if page is not None else -1,
                        "char_start": char_cursor,
                        "char_end": char_cursor + len(piece),
                    },
                ))
                chunk_index += 1
                char_cursor += len(piece)
        return chunks

    def _split_by_structure(self, text: str) -> list[tuple[str, list[str], int]]:
        """按 Markdown 标题 + 代码块结构切分

        Returns:
            [(block_text, heading_path, offset), ...]
            heading_path: 当前块所属的标题层级路径（如 ["安装", "依赖"]）
            offset: 块在原文中的近似起始偏移
        """
        # 先保护代码块：把 ``` 围栏内的内容整体提取，避免被标题/段落规则切碎
        code_spans: list[tuple[int, int]] = []
        for m in _FENCE_RE.finditer(text):
            fence = m.group(1)
            start = m.start()
            # 找配对的闭合围栏
            close_m = re.search(rf"^{re.escape(fence)}\s*$", text[m.end():], re.MULTILINE)
            if close_m:
                end = m.end() + close_m.end()
                code_spans.append((start, end))

        def _in_code(pos: int) -> bool:
            return any(s <= pos < e for s, e in code_spans)

        # 按标题切分，维护 heading_path 栈
        blocks: list[tuple[str, list[str], int]] = []
        heading_stack: list[tuple[int, str]] = []  # (level, title)
        last_pos = 0

        for m in _HEADING_RE.finditer(text):
            if _in_code(m.start()):
                continue  # 代码块内的 # 不当标题
            # 先收集上一个标题到当前标题之间的内容
            if m.start() > last_pos:
                seg = text[last_pos:m.start()]
                if seg.strip():
                    path = [t for _, t in heading_stack]
                    blocks.append((seg, list(path), last_pos))
            # 更新标题栈
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            last_pos = m.end()

        # 收集最后一个标题之后的内容
        if last_pos < len(text):
            seg = text[last_pos:]
            if seg.strip():
                path = [t for _, t in heading_stack]
                blocks.append((seg, list(path), last_pos))

        # 无任何标题结构 → 整体作为一个块
        if not blocks:
            blocks.append((text, [], 0))

        # 代码块整体作为独立块追加（保证不被腰斩）
        for s, e in code_spans:
            blocks.append((text[s:e], ["<code>"], s))

        # 简单去重：若代码块已被标题块包含则跳过
        deduped: list[tuple[str, list[str], int]] = []
        for content, path, off in blocks:
            if any(content == c2 for c2, _, _ in deduped):
                continue
            deduped.append((content, path, off))
        return deduped

    def split_text_parent_child(
        self,
        text: str,
        *,
        source: str = "",
        doc_id: str = "",
        page: int | None = None,
        parent_size: int = 2000,
        child_size: int = 500,
        child_overlap: int = 80,
    ) -> tuple[list[Chunk], list[Chunk]]:
        """父子分块：parent 大块保上下文，child 小块供向量检索

        Args:
            text: 原始文本
            source/doc_id/page: 元数据
            parent_size: 父块最大字符数（结构感知切分，不向量化）
            child_size: 子块最大字符数（对父块递归字符切，向量化检索）
            child_overlap: 子块切分重叠字符数

        Returns:
            (parents, children)
            - parents: 父块 Chunk，metadata 含 parent_index
            - children: 子块 Chunk，metadata 含 parent_index（可拼 parent_id）
        """
        if not text or not text.strip():
            return [], []

        # 第一层：按结构切成 parent 级段落块
        parent_blocks = self._split_by_structure(text)

        # 父块若超 parent_size，用更宽松的字符切再切一层
        parent_splitter = RecursiveCharacterTextSplitter(
            chunk_size=max(1, parent_size),
            chunk_overlap=0,
            separators=self.separators,
            keep_separator=True,
        )
        # 子块字符切分器
        child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=max(1, child_size),
            chunk_overlap=max(0, min(child_overlap, child_size - 1)),
            separators=self.separators,
            keep_separator=True,
        )

        parents: list[Chunk] = []
        children: list[Chunk] = []
        parent_index = 0
        chunk_cursor = 0

        for block_content, heading_path, block_offset in parent_blocks:
            # 父块降级切分
            if len(block_content) > parent_size:
                parent_pieces = parent_splitter.split_text(block_content)
            else:
                parent_pieces = [block_content] if block_content.strip() else []

            for p_piece in parent_pieces:
                p_piece = p_piece.strip()
                if not p_piece:
                    continue
                base_meta = {
                    "source": source,
                    "doc_id": doc_id,
                    "parent_index": parent_index,
                    "heading_path": " / ".join(heading_path) if heading_path else "",
                    "page": page if page is not None else -1,
                    "char_start": chunk_cursor,
                    "char_end": chunk_cursor + len(p_piece),
                }
                parents.append(Chunk(content=p_piece, metadata=dict(base_meta)))

                # 子块：对父块再细切
                if len(p_piece) > child_size:
                    child_pieces = child_splitter.split_text(p_piece)
                else:
                    child_pieces = [p_piece]

                for child_piece in child_pieces:
                    child_piece = child_piece.strip()
                    if not child_piece:
                        continue
                    child_meta = dict(base_meta)
                    child_meta["chunk_index"] = len(children)
                    children.append(Chunk(content=child_piece, metadata=child_meta))

                chunk_cursor += len(p_piece)
                parent_index += 1

        return parents, children
