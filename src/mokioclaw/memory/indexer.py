"""
Memory 索引器

为 workspace 中的记忆文件（TODO.md, NOTEPAD.md, HISTORY_SUMMARY.md, RAW_HISTORY.md）
建立持久化索引，支持渐进式披露和关键词检索。

索引文件：{workspace}/.mokioclaw/memory_index.json

设计原则：
- 启动时只读 index（~200 tokens），不加载完整文件
- 需要时按需读取完整内容
- 搜索时用 index 定位，再读取对应 section
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_FILES = ("TODO.md", "NOTEPAD.md", "HISTORY_SUMMARY.md", "RAW_HISTORY.md")
MEMORY_INDEX_FILE = ".mokioclaw/memory_index.json"
MAX_INDEX_CHARS_PER_FIELD = 500


@dataclass
class MemoryIndex:
    """Memory 索引数据结构"""
    version: int = 1
    updated_at: str = ""
    workspace: str = ""
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "workspace": self.workspace,
            "files": self.files,
            "keywords": self.keywords,
            "sections": self.sections,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryIndex:
        return cls(
            version=data.get("version", 1),
            updated_at=data.get("updated_at", ""),
            workspace=data.get("workspace", ""),
            files=data.get("files", {}),
            keywords=data.get("keywords", {}),
            sections=data.get("sections", []),
        )


def build_memory_index(workspace: Path) -> MemoryIndex:
    """扫描 workspace 的记忆文件，构建索引

    Args:
        workspace: 工作区路径

    Returns:
        MemoryIndex 对象
    """
    index = MemoryIndex(
        updated_at=datetime.now(timezone.utc).isoformat(),
        workspace=str(workspace),
    )

    for filename in MEMORY_FILES:
        file_path = workspace / filename
        if not file_path.exists():
            index.files[filename] = {"exists": False}
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            stat = file_path.stat()
        except OSError:
            index.files[filename] = {"exists": False}
            continue

        file_info: dict[str, Any] = {
            "exists": True,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
            "total_chars": len(content),
        }

        # 文件特定统计
        if filename == "TODO.md":
            file_info.update(_index_todo(content))
        elif filename == "NOTEPAD.md":
            file_info.update(_index_notepad(content))
        elif filename == "HISTORY_SUMMARY.md":
            file_info["turn_range"] = _extract_turn_range(content)
        elif filename == "RAW_HISTORY.md":
            file_info["message_count"] = content.count("## ")

        index.files[filename] = file_info

        # 提取 section 信息（heading + 起始位置）+ 每个 section 的关键词
        headings = list(re.finditer(r"^(#{1,3})\s+(.+)$", content, re.MULTILINE))
        for idx, match in enumerate(headings):
            level = len(match.group(1))
            heading = match.group(2).strip()
            start = match.start()
            line_num = content[:start].count("\n") + 1
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(content)
            section_text = content[start:end]

            index.sections.append({
                "file": filename,
                "heading": heading,
                "level": level,
                "start_line": line_num,
                "chars": min(len(section_text), MAX_INDEX_CHARS_PER_FIELD),
            })

            # 从该 section 的文本中提取关键词
            words = _extract_keywords(section_text)
            for word in words:
                ref = f"{filename}#L{line_num}"
                if word not in index.keywords:
                    index.keywords[word] = []
                if ref not in index.keywords[word]:
                    index.keywords[word].append(ref)

    # 限制 section 数量
    index.sections = index.sections[:200]
    # 限制关键词数量
    if len(index.keywords) > 500:
        index.keywords = dict(list(index.keywords.items())[:500])

    return index


def _index_todo(content: str) -> dict[str, Any]:
    """索引 TODO.md 内容"""
    todos = re.findall(r"^- \[([ x!-])\]", content, re.MULTILINE)
    return {
        "todo_count": len(todos),
        "completed": sum(1 for s in todos if s == "x"),
        "in_progress": sum(1 for s in todos if s == "-"),
        "blocked": sum(1 for s in todos if s == "!"),
    }


def _index_notepad(content: str) -> dict[str, Any]:
    """索引 NOTEPAD.md 内容"""
    sections = re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
    return {
        "section_count": len(sections),
        "headings": sections[:20],
    }


def _extract_turn_range(content: str) -> str:
    """从 HISTORY_SUMMARY.md 提取轮次范围"""
    match = re.search(r"turns?\s+(\d+)[-–](\d+)", content, re.IGNORECASE)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return ""


def _extract_keywords(content: str, max_keywords: int = 100) -> list[str]:
    """从内容中提取关键词（简单实现）"""
    # 中文：连续 2-10 个中文字符
    chinese = re.findall(r"[一-鿿]{2,10}", content)
    # 英文：长度 >= 4 的单词，排除常见停用词
    stop_words = {"the", "and", "for", "with", "this", "that", "from", "have", "been", "will", "task", "plan"}
    english = [
        w.lower() for w in re.findall(r"[A-Za-z]{4,}", content)
        if w.lower() not in stop_words
    ]

    # 去重并限制数量
    seen: set[str] = set()
    keywords: list[str] = []
    for word in chinese + english:
        if word not in seen:
            seen.add(word)
            keywords.append(word)
        if len(keywords) >= max_keywords:
            break

    return keywords


def load_memory_index(workspace: Path) -> MemoryIndex | None:
    """加载已有的 memory 索引

    Args:
        workspace: 工作区路径

    Returns:
        MemoryIndex 或 None（索引不存在）
    """
    index_path = workspace / MEMORY_INDEX_FILE
    if not index_path.exists():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return MemoryIndex.from_dict(data)
    except (OSError, json.JSONDecodeError):
        return None


def save_memory_index(workspace: Path, index: MemoryIndex) -> None:
    """保存 memory 索引到 workspace

    Args:
        workspace: 工作区路径
        index: MemoryIndex 对象
    """
    index_path = workspace / MEMORY_INDEX_FILE
    index_path.parent.mkdir(parents=True, exist_ok=True)
    data = index.to_dict()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        import logging
        logging.getLogger(__name__).warning("failed to save memory index: %s", exc)


def search_memory_index(
    workspace: Path,
    query: str,
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """在 memory 索引中搜索

    渐进式披露：先用 index 定位，再读取对应内容。

    Args:
        workspace: 工作区路径
        query: 搜索关键词
        top_k: 返回前 k 个结果

    Returns:
        搜索结果字典：{files: [...], sections: [...], matches: [...]}
    """
    index = load_memory_index(workspace)
    if index is None:
        return {"files": [], "sections": [], "matches": [], "note": "no index found"}

    query_lower = query.lower()
    matched_files: dict[str, float] = {}
    matched_sections: list[dict[str, Any]] = []
    matched_keywords: list[str] = []

    # 关键词匹配
    for word, refs in index.keywords.items():
        if query_lower in word.lower() or word.lower() in query_lower:
            matched_keywords.append(word)
            for ref in refs:
                filename = ref.split("#")[0]
                matched_files[filename] = matched_files.get(filename, 0) + 1

    # Section 匹配
    for section in index.sections:
        if query_lower in section["heading"].lower():
            matched_sections.append(section)
            filename = section["file"]
            matched_files[filename] = matched_files.get(filename, 0) + 1

    # 排序并取 top_k
    sorted_files = sorted(matched_files.items(), key=lambda x: -x[1])[:top_k]

    # 渐进式披露：只返回元数据，不加载完整文件
    results = {
        "query": query,
        "matched_keywords": matched_keywords[:20],
        "files": [
            {
                "name": name,
                "score": score,
                "info": index.files.get(name, {}),
            }
            for name, score in sorted_files
        ],
        "sections": matched_sections[:top_k],
        "note": "use file paths to read full content",
    }

    return results
