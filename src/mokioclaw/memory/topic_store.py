"""
主题记忆文件管理 — 索引 + 主题文件分离模式

MEMORY.md 作为轻量索引（指针清单），注入 system prompt。
详细记忆内容分散在各个 xxx.md 主题文件中，模型按需 read 读取。

这避免了"每次会话把全部记忆塞进 token 窗口"的问题。

存储结构：
    .mokioclaw/memory/
    ├── MEMORY.md              # 索引文件（截断 200 行 / 25KB）
    ├── user_preferences.md    # 主题文件（带 YAML frontmatter）
    ├── project_decisions.md
    ├── feedback.md
    └── ...
"""
from __future__ import annotations

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

# 主题类型
TOPIC_TYPES = ("user", "feedback", "project", "reference")

# 主题名合法字符（用于防路径遍历，review #15）：字母/数字/下划线/连字符/中文
_TOPIC_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-一-鿿]+$")


def _safe_topic_name(name: str) -> str | None:
    """净化主题名，拒绝含路径分隔符/遍历段的名字。

    主题名用作文件名，必须禁止 ``..``/``/``/``\\`` 等可能导致越界写入
    memory_dir 之外的字符。返回净化后的名字或 None（非法）。
    """
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if not _TOPIC_NAME_PATTERN.match(name) or name in {".", ".."}:
        return None
    return name


@dataclass
class TopicMeta:
    """主题文件元数据（从 YAML frontmatter 解析）"""
    name: str
    description: str
    topic_type: str  # user / feedback / project / reference
    file_path: Path


class TopicStore:
    """主题记忆文件管理器

    管理 .mokioclaw/memory/ 目录下的主题记忆文件。
    提供索引加载、主题读写、索引更新等功能。
    """

    def __init__(self, workspace: Path) -> None:
        self.memory_dir = workspace / ".mokioclaw" / "memory"
        # 进程内写锁：守护线程（autodream/background extraction）与主线程并发写
        # MEMORY.md 时保护 read-modify-write 原子性，避免 lost-update 丢索引指针（m8）。
        self._write_lock = threading.Lock()

    def ensure_dir(self) -> None:
        """确保 memory 目录存在"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> str:
        """加载 MEMORY.md 索引，截断到限制范围内

        Returns:
            索引文本内容。文件不存在时返回空字符串。
        """
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
        """扫描 memory 目录，解析所有主题文件的 frontmatter

        Returns:
            主题元数据列表
        """
        if not self.memory_dir.exists():
            return []
        topics: list[TopicMeta] = []
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == MEMORY_INDEX_FILE:
                continue
            meta = self._parse_frontmatter(md_file)
            if meta:
                topics.append(meta)
        return topics

    def read_topic(self, name: str) -> dict[str, Any]:
        """读取单个主题文件的完整内容

        Args:
            name: 主题名（不含 .md 后缀）

        Returns:
            包含 ok, content, exists 的字典
        """
        safe = _safe_topic_name(name)
        if safe is None:
            return {"ok": False, "content": "", "exists": False, "error": "invalid topic name"}
        path = self.memory_dir / f"{safe}.md"
        if not path.exists():
            return {"ok": True, "content": "", "exists": False}
        try:
            content = path.read_text(encoding="utf-8")
            return {"ok": True, "content": content, "exists": True, "path": str(path)}
        except OSError as exc:
            logger.debug("Failed to read topic '%s': %s", name, exc)
            return {"ok": False, "content": "", "exists": True, "error": str(exc)}

    def write_topic(
        self,
        name: str,
        content: str,
        topic_type: str = "project",
        description: str = "",
    ) -> dict[str, Any]:
        """写入或更新主题文件，并更新索引

        Args:
            name: 主题名（不含 .md 后缀）
            content: 主题内容
            topic_type: 类型（user/feedback/project/reference）
            description: 一行描述

        Returns:
            操作结果字典
        """
        self.ensure_dir()
        if topic_type not in TOPIC_TYPES:
            topic_type = "project"

        safe = _safe_topic_name(name)
        if safe is None:
            return {"ok": False, "error": "invalid topic name"}
        path = self.memory_dir / f"{safe}.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # description 去换行（m7）：含换行会破坏 YAML frontmatter 逐行解析
        safe_description = (description or safe).replace("\r", " ").replace("\n", " ").strip() or safe

        # 构造带 frontmatter 的内容（description 已净化，值无需引号转义）
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
                path.write_text(full_content, encoding="utf-8")
            except OSError as exc:
                logger.warning("Failed to write topic '%s': %s", safe, exc)
                return {"ok": False, "error": str(exc)}

            # 更新索引（锁内，原子 read-modify-write）
            self._update_index(safe, safe_description, topic_type)

        return {"ok": True, "path": str(path), "name": safe}

    def _update_index(self, name: str, description: str, topic_type: str) -> None:
        """更新 MEMORY.md 索引，添加或更新一条指针

        原子写：先写临时文件再 os.replace，避免并发写时读到半截内容（m8）。
        调用方持 _write_lock。
        """
        import os

        index_path = self.memory_dir / MEMORY_INDEX_FILE
        entry_pattern = re.compile(rf"^- \[{re.escape(name)}\]", re.MULTILINE)
        new_entry = f"- [{name}]({name}.md) — {description}"

        if index_path.exists():
            try:
                existing = index_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        else:
            existing = "# Memory Index\n\n"

        if entry_pattern.search(existing):
            updated = entry_pattern.sub(new_entry, existing)
        else:
            updated = existing.rstrip() + "\n" + new_entry + "\n"

        try:
            tmp_path = index_path.with_suffix(".md.tmp")
            tmp_path.write_text(updated, encoding="utf-8")
            os.replace(tmp_path, index_path)
        except OSError as exc:
            logger.warning("Failed to update memory index: %s", exc)

    def _parse_frontmatter(self, path: Path) -> TopicMeta | None:
        """解析 markdown 文件的 YAML frontmatter"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            # 没有 frontmatter，用文件名作为元数据
            name = path.stem
            return TopicMeta(
                name=name,
                description=name,
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
        """截断索引到限制范围内"""
        lines = content.splitlines()
        if len(lines) > MAX_INDEX_LINES:
            lines = lines[:MAX_INDEX_LINES]
            lines.append(f"\n... [truncated, {len(content.splitlines())} lines total]")
        result = "\n".join(lines)
        if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
            result = result[:MAX_INDEX_BYTES] + "\n... [truncated]"
        return result
