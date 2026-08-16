"""
主题记忆文件管理 — 单层索引 + 主题文件模式

存储结构：
    .mokioclaw/memory/
    ├── MEMORY.md              # 索引文件（截断 200 行 / 25KB）
    ├── feedback_xxx.md        # 主题文件（带 YAML frontmatter）
    ├── user_xxx.md
    ├── project_xxx.md
    └── reference_xxx.md

设计对齐 Claude Code memory.md：
- MEMORY.md 只存索引行，不重复正文
- 主题正文按需 read，避免每次会话塞满 token
- 文件名带 type 前缀，一眼可区分类别
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import truncate

logger = get_logger(__name__)

# 索引文件名
MEMORY_INDEX_FILE = "MEMORY.md"

# 索引截断限制
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024  # 25KB

# 主题类型 + 文件前缀映射
TOPIC_TYPES = ("user", "feedback", "project", "reference")
_TOPIC_PREFIXES = {
    "feedback": "feedback_",
    "user": "user_",
    "project": "project_",
    "reference": "reference_",
}

# 主题名合法字符：字母/数字/下划线/连字符/中文
_TOPIC_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-一-鿿]+$")


def _safe_topic_name(name: str) -> str | None:
    """净化主题名，拒绝含路径遍历段的名字。"""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if not _TOPIC_NAME_PATTERN.match(name) or name in {".", ".."}:
        return None
    return name


def _apply_prefix(name: str, topic_type: str) -> str:
    """确保文件名带 type 前缀。已带前缀的保持原样。"""
    prefix = _TOPIC_PREFIXES.get(topic_type, "project_")
    if name.startswith(tuple(_TOPIC_PREFIXES.values())):
        return name
    return f"{prefix}{name}"


@dataclass
class TopicMeta:
    """主题文件元数据（从 YAML frontmatter 解析）"""
    name: str
    description: str
    topic_type: str
    file_path: Path


class TopicStore:
    """主题记忆文件管理器

    管理 .mokioclaw/memory/ 目录下的主题记忆文件。
    单层存储，文件名带 type 前缀。
    """

    def __init__(self, workspace: Path) -> None:
        self.memory_dir = workspace / ".mokioclaw" / "memory"
        self._write_lock = threading.Lock()

    def ensure_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> str:
        """加载 MEMORY.md 索引。文件不存在返回空字符串。"""
        index_path = self.memory_dir / MEMORY_INDEX_FILE
        if not index_path.exists():
            return ""
        try:
            content = index_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Failed to read memory index: %s", exc)
            return ""
        return self._truncate_index(content)

    def list_topics(self) -> list[TopicMeta]:
        """扫描 memory 目录，解析所有主题文件的 frontmatter。"""
        topics: list[TopicMeta] = []
        if not self.memory_dir.exists():
            return topics
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == MEMORY_INDEX_FILE:
                continue
            meta = self._parse_frontmatter(md_file)
            if meta:
                topics.append(meta)
        return topics

    def read_topic(self, name: str) -> dict[str, Any]:
        """读取单个主题文件的完整内容。

        Args:
            name: 主题名（可带或不带 type 前缀，不带则自动推断）
        """
        safe = _safe_topic_name(name)
        if safe is None:
            return {"ok": False, "content": "", "exists": False, "error": "invalid topic name"}

        # 先按给定名字找
        path = self.memory_dir / f"{safe}.md"
        if not path.exists():
            # 尝试补各类型前缀
            for prefix in _TOPIC_PREFIXES.values():
                candidate = self.memory_dir / f"{prefix}{safe}.md"
                if candidate.exists():
                    path = candidate
                    break
        if not path.exists():
            return {"ok": True, "content": "", "exists": False}

        try:
            content = path.read_text(encoding="utf-8")
            return {"ok": True, "content": content, "exists": True, "path": str(path)}
        except OSError as exc:
            logger.debug("Failed to read topic '%s': %s", name, exc)
            return {"ok": True, "content": "", "exists": False}

    def write_topic(
        self,
        name: str,
        content: str,
        topic_type: str = "project",
        description: str = "",
    ) -> dict[str, Any]:
        """写入或更新主题文件，并更新索引。

        Args:
            name: 主题名（不含 .md 后缀，可带或不带 type 前缀）
            content: 主题内容
            topic_type: 类型（user/feedback/project/reference）
            description: 一行描述
        """
        self.ensure_dir()

        if topic_type not in TOPIC_TYPES:
            topic_type = "project"

        # 应用文件名前缀
        prefixed_name = _apply_prefix(name, topic_type)
        safe = _safe_topic_name(prefixed_name)
        if safe is None:
            return {"ok": False, "error": "invalid topic name"}

        path = self.memory_dir / f"{safe}.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_description = (description or safe).replace("\r", " ").replace("\n", " ").strip() or safe

        frontmatter = (
            f"---\n"
            f"name: {safe}\n"
            f"description: {safe_description}\n"
            f"type: {topic_type}\n"
            f"updated: {timestamp}\n"
            f"---\n\n"
        )
        full_content = frontmatter + content.strip() + "\n"

        with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(full_content, encoding="utf-8")
            except OSError as exc:
                logger.warning("Failed to write topic '%s': %s", safe, exc)
                return {"ok": False, "error": str(exc)}
            self._update_index(safe, safe_description, topic_type)

        return {"ok": True, "path": str(path), "name": safe}

    def _update_index(self, name: str, description: str, topic_type: str) -> None:
        """更新 MEMORY.md 索引。调用方持 _write_lock。"""
        index_path = self.memory_dir / MEMORY_INDEX_FILE
        new_entry = f"- [{name}]({name}.md) — {description}"

        if index_path.exists():
            try:
                existing = index_path.read_text(encoding="utf-8")
            except OSError:
                existing = "# Memory Index\n\n"
        else:
            existing = "# Memory Index\n\n"

        # 移除旧条目（匹配任意前缀的同名项）
        old_pattern = re.compile(rf"^- \[{re.escape(name)}\]", re.MULTILINE)
        cleaned = old_pattern.sub("", existing)
        # 也移除可能存在的无前缀同名条目
        base = name
        for prefix in _TOPIC_PREFIXES.values():
            if name.startswith(prefix):
                base = name[len(prefix):]
                break
        old_base_pattern = re.compile(rf"^- \[{re.escape(base)}\]", re.MULTILINE)
        cleaned = old_base_pattern.sub("", cleaned)

        updated = cleaned.rstrip() + "\n" + new_entry + "\n"
        # 清理多余空行
        updated = re.sub(r"\n{3,}", "\n\n", updated)

        try:
            tmp_path = index_path.with_suffix(".md.tmp")
            tmp_path.write_text(updated, encoding="utf-8")
            os.replace(tmp_path, index_path)
        except OSError as exc:
            logger.warning("Failed to update memory index: %s", exc)

    def _parse_frontmatter(self, path: Path) -> TopicMeta | None:
        """解析 markdown 文件的 YAML frontmatter。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            return TopicMeta(
                name=path.stem,
                description=path.stem,
                topic_type="project",
                file_path=path,
            )
        fm_text = match.group(1)
        meta: dict[str, str] = {}
        for line in fm_text.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        return TopicMeta(
            name=meta.get("name", path.stem),
            description=meta.get("description", path.stem),
            topic_type=meta.get("type", "project"),
            file_path=path,
        )

    def _truncate_index(self, content: str) -> str:
        """截断索引到限制范围内。"""
        lines = content.splitlines()
        if len(lines) > MAX_INDEX_LINES:
            lines = lines[:MAX_INDEX_LINES]
            lines.append(f"\n... [truncated, {len(content.splitlines())} lines total]")
        result = "\n".join(lines)
        if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
            result = result[:MAX_INDEX_BYTES] + "\n... [truncated]"
        return result
