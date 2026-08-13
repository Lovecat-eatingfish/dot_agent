"""
自动记忆提取 + AutoDream

对齐 Claude Code：
1. 用户显式「记住」→ 主 Agent 直接写 topic
2. 每轮结束后后台提取（禁止 bash/MCP/再派子 Agent，最多 5 轮）
3. AutoDream：距上次 ≥24h 且累计 ≥5 次会话时整理记忆
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import truncate, write_json, parse_json_content
from mokioclaw.memory.topic_store import TopicStore
from mokioclaw.reliability.background_tasks import run_in_thread

logger = get_logger(__name__)

_REMEMBER_PATTERNS = [
    re.compile(r"(?:请)?记住[:：]\s*(.+)", re.I),
    re.compile(r"(?:please\s+)?remember[:：]\s*(.+)", re.I),
]

_DREAM_META = "autodream.json"
_EXTRACT_CURSOR = "extract_cursor.json"

# 进程内锁：守护线程（autodream / background extraction）与主线程并发写
# autodream.json 及 MEMORY.md 时保护 read-modify-write，避免 lost-update（m2/m8）。
# 模块级单例覆盖同一 workspace 的所有写入路径。
_dream_meta_lock = threading.Lock()


def maybe_write_explicit_memory(workspace: Path, user_text: str) -> dict[str, Any] | None:
    """用户显式要求记住时同步写入主题记忆"""
    text = (user_text or "").strip()
    content = ""
    for pattern in _REMEMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            content = match.group(1).strip()
            break
    if not content:
        return None
    store = TopicStore(workspace)
    name = f"feedback_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = store.write_topic(
        name=name,
        content=content,
        topic_type="feedback",
        description=truncate(content, 80),
    )
    return result


def trigger_background_extraction(
    workspace: Path,
    *,
    new_messages: list[str],
    session_id: str = "",
) -> None:
    """对话结束后后台提取记忆（尽力、静默失败）

    优先用 LLM 做语义提取（对齐 Claude Code 的 forked 后台子 Agent），
    LLM 不可用或解析失败时回退到正则提取。
    """
    if not new_messages:
        return

    def _worker() -> None:
        try:
            ok = extract_with_model(workspace, new_messages=new_messages, session_id=session_id)
            if not ok:
                _extract_memories_regex(workspace, new_messages=new_messages, session_id=session_id)
        except Exception as exc:
            logger.debug("background memory extraction failed: %s", exc)

    run_in_thread(_worker)


def trigger_autodream_if_needed(workspace: Path) -> None:
    """满足条件时后台整理记忆"""
    def _worker() -> None:
        try:
            if _should_dream(workspace):
                _run_autodream(workspace)
        except Exception as exc:
            logger.debug("autodream failed: %s", exc)

    run_in_thread(_worker)


# 最多提取的主题条数
_MAX_EXTRACTED_TOPICS = 8

# LLM 记忆提取器 prompt（对齐 Claude Code：禁止 bash/MCP/再派子 Agent 的 forked 子 Agent，
# 只从本轮新增消息里提取可持久化的偏好/决策/踩坑）
_EXTRACT_PROMPT = """You are a memory extraction worker for an AI coding agent.

You are NOT the main agent. You do not have tools. Read the transcript below and extract ONLY durable facts worth remembering across sessions:
- user: stable user preferences / working style (e.g. "user dislikes Lombok", "always use tabs")
- project: architecture / tech-stack decisions (e.g. "backend is FastAPI", "tests use pytest")
- feedback: pitfalls / gotchas / corrections learned this turn

Rules:
- Skip ephemeral task state, transient questions, and anything already obvious from a project's CLAUDE.md.
- Each topic: a short slug name (lowercase, words joined by _), the content as one concise sentence, and a type (user|project|feedback).
- Do NOT invent facts. If there is nothing worth saving, return an empty list.
- Output ONLY a JSON object, no prose: {"topics":[{"name":"...","content":"...","type":"..."}]}

Transcript:
"""


def extract_with_model(
    workspace: Path,
    *,
    new_messages: list[str],
    session_id: str = "",
) -> bool:
    """用 LLM 从本轮新增消息里语义提取记忆主题，写入 TopicStore。

    对齐 Claude Code 后台记忆提取子 Agent：
    - 不 bind 任何工具（满足「禁止 bash / MCP / 再派子 Agent」沙箱约束）
    - 静默尽力：LLM 不可用 / 返回非 JSON / 无可提取内容 → 返回 False，由调用方回退到正则
    - 去重：name 或 content 已存在于 MEMORY.md 索引则跳过
    - 最多写 _MAX_EXTRACTED_TOPICS 条
    """
    try:
        from mokioclaw.providers.openai_provider import create_model

        model = create_model()
    except Exception as exc:
        logger.debug("memory extraction model unavailable: %s", exc)
        return False

    blob = "\n".join(new_messages)
    prompt = f"{_EXTRACT_PROMPT}{blob}"
    try:
        response = model.invoke(prompt)
    except Exception as exc:
        logger.debug("memory extraction invoke failed: %s", exc)
        return False

    text = getattr(response, "content", response)
    if not isinstance(text, str):
        text = str(text)
    parsed = parse_json_content(text)
    if not isinstance(parsed, dict):
        return False
    raw_topics = parsed.get("topics")
    if not isinstance(raw_topics, list):
        return False

    store = TopicStore(workspace)
    existing = store.load_index().lower()
    written = 0
    for item in raw_topics[:_MAX_EXTRACTED_TOPICS]:
        if not isinstance(item, dict):
            continue
        name = _slug(str(item.get("name", "")).strip())
        content = str(item.get("content", "")).strip()
        topic_type = str(item.get("type", "project")).strip().lower()
        if not name or not content:
            continue
        if topic_type not in {"user", "project", "feedback", "reference"}:
            topic_type = "project"
        if name.lower() in existing or content.lower() in existing:
            continue
        store.write_topic(name=name, content=content, topic_type=topic_type, description=truncate(content, 80))
        # 刷新索引快照，使循环内后续迭代能对本次新写入的条目去重（m6）
        existing = store.load_index().lower()
        written += 1

    _bump_session_counter(workspace, session_id=session_id)
    if written:
        logger.info("Auto-memory (LLM) extracted %d topics", written)
    return True


def _extract_memories_regex(workspace: Path, *, new_messages: list[str], session_id: str) -> None:
    """规则级提取回退：偏好 / 决策 / 踩坑（不调用 LLM）

    当 LLM 提取不可用时使用，对齐 Claude Code 的兜底策略。
    """
    store = TopicStore(workspace)
    blob = "\n".join(new_messages)
    extracted: list[tuple[str, str, str]] = []

    for pattern, topic_type, prefix in (
        (r"(?:prefer|偏好|不要使用|禁止使用|always use|永远使用)[^\n。.!?]{4,120}", "user", "pref"),
        (r"(?:决定|decision|采用|改用|architecture)[^\n。.!?]{4,120}", "project", "decision"),
        (r"(?:踩坑|注意|坑点|warning|gotcha)[^\n。.!?]{4,120}", "feedback", "pitfall"),
    ):
        for match in re.finditer(pattern, blob, flags=re.I):
            snippet = match.group(0).strip()
            extracted.append((f"{prefix}_{_slug(snippet)[:40]}", snippet, topic_type))

    # 去重：已有索引条目则跳过。循环内刷新快照，避免对本次新写入条目重复写（m6）。
    existing = store.load_index().lower()
    written = 0
    for name, content, topic_type in extracted[:_MAX_EXTRACTED_TOPICS]:
        if name.lower() in existing or content.lower() in existing:
            continue
        store.write_topic(name=name, content=content, topic_type=topic_type, description=truncate(content, 80))
        existing = store.load_index().lower()
        written += 1

    _bump_session_counter(workspace, session_id=session_id)
    if written:
        logger.info("Auto-memory extracted %d topics", written)


def _should_dream(workspace: Path) -> bool:
    meta_path = workspace / ".mokioclaw" / "memory" / _DREAM_META
    meta = _read_json(meta_path)
    last = meta.get("last_dream_at")
    sessions = int(meta.get("sessions_since_dream", 0) or 0)
    if sessions < 5:
        return False
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= 24 * 3600


def _run_autodream(workspace: Path) -> None:
    """合并重复索引、截断 MEMORY.md、拆分过长主题（规则级 GC）"""
    store = TopicStore(workspace)
    store.ensure_dir()
    topics = store.list_topics()
    lines = ["# Memory Index", ""]
    for topic in topics[:180]:
        lines.append(f"- [{topic.name}]({topic.name}.md) — {topic.description}")
    index_path = store.memory_dir / "MEMORY.md"
    # 与 write_topic 走同一把进程内锁，避免与并发写 MEMORY.md 的 lost-update（m8）
    with store._write_lock:
        import os

        tmp_path = index_path.with_suffix(".md.tmp")
        try:
            tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp_path, index_path)
        except OSError as exc:
            logger.warning("autodream index rebuild failed: %s", exc)

    # 截断超长主题正文（保留 frontmatter + 前 8KB）
    for topic in topics:
        try:
            text = topic.file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text.encode("utf-8")) > 16_000:
            trimmed = text[:12_000] + "\n\n... [autodream truncated]\n"
            topic.file_path.write_text(trimmed, encoding="utf-8")

    meta_path = store.memory_dir / _DREAM_META
    with _dream_meta_lock:
        write_json(
            meta_path,
            {
                "last_dream_at": datetime.now(timezone.utc).isoformat(),
                "sessions_since_dream": 0,
                "topics": len(topics),
            },
        )
    logger.info("AutoDream compacted %d topics", len(topics))


def _bump_session_counter(workspace: Path, *, session_id: str) -> None:
    meta_path = workspace / ".mokioclaw" / "memory" / _DREAM_META
    # 与 _run_autodream 走同一把锁，避免与守护线程 autodream 写 lost-update（m2）
    with _dream_meta_lock:
        meta = _read_json(meta_path)
        seen = set(meta.get("seen_sessions") or [])
        if session_id and session_id not in seen:
            seen.add(session_id)
            meta["seen_sessions"] = list(seen)[-50:]
            meta["sessions_since_dream"] = int(meta.get("sessions_since_dream", 0) or 0) + 1
            write_json(meta_path, meta)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", text).strip("_")
    return slug or "note"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
