"""
分层记忆系统

实现了三层记忆机制，帮助智能体在长对话中保持上下文：

1. 规则层（rules）：
   - 持久化的工作规则
   - 跨任务保持不变
   - 定义工作区约束和行为准则

2. 工作记忆层（working_memory）：
   - 当前任务的关键信息
   - 包含任务、计划、待办事项、研究笔记等
   - 随任务进展动态更新

3. 历史摘要层（history_summary_store）：
   - 过往对话的压缩总结
   - 保存在 HISTORY_SUMMARY.md 文件中
   - 上下文压缩时自动更新

存储文件：
- TODO.md: 待办事项和计划
- NOTEPAD.md: 持久化笔记
- HISTORY_SUMMARY.md: 历史对话摘要
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.memory.topic_store import TopicStore
from mokioclaw.security.path_security import PathSecurityError
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.core.utils import truncate, trim_handoffs
from mokioclaw.tools.file_tools import read_text_lossy
from mokioclaw.tools.notepad_tool import NOTEPAD_FILE, read_notepad

logger = get_logger(__name__)

# 历史摘要文件名
HISTORY_SUMMARY_FILE = "HISTORY_SUMMARY.md"

# 规则层配置：定义智能体的工作规则
RULES_LAYER = {
    "scope": "workspace",
    "storage": "internal",
    "rules": [
        "Work inside the current workspace only.",
        "Use paths relative to the workspace; do not prefix paths with workspace/.",
        "Keep durable task context outside the raw messages transcript when possible.",
        "Treat TODO.md as working plan state, NOTEPAD.md as durable notes, and HISTORY_SUMMARY.md as compressed history.",
        "Do not expose memory write tools to agents; layered memory is assembled by the runtime.",
    ],
}

# 各字段的最大字符数限制，防止上下文溢出
MAX_TEXT_CHARS = {
    "research_notes": 1600,          # 研究笔记
    "agent_handoff_instruction": 500, # 智能体交接指令
    "agent_handoff_result": 700,     # 智能体交接结果
    "code_agent_summary": 1000,      # 代码智能体摘要
    "verifier_summary": 1000,        # 校验器摘要
    "last_error": 1400,              # 最近错误
    "context_summary": 1600,         # 上下文摘要
    "session_context": 1800,         # 会话上下文
    "notepad": 1800,                 # 笔记本内容
    "history_summary": 2200,         # 历史摘要
}


def build_layered_memory(state: dict[str, Any], *, node: str = "graph") -> dict[str, Any]:
    """构建分层记忆结构

    从当前状态和持久化文件中组装三层记忆：
    1. 规则层：静态配置
    2. 工作记忆：当前任务的动态信息
    3. 历史摘要：过往对话的压缩总结

    Args:
        state: 当前工作流状态
        node: 当前节点名称，用于标记记忆来源

    Returns:
        分层记忆字典，包含 rules, working_memory, history_summary_store
    """
    runtime = state["runtime"]
    try:
        notepad = read_notepad(runtime)
    except Exception as exc:
        logger.debug("notepad read failed: %s", exc)
        notepad = {"ok": False, "content": "", "exists": False}
    try:
        history = read_history_summary(runtime)
    except Exception as exc:
        logger.debug("history summary read failed: %s", exc)
        history = {"ok": False, "content": "", "exists": False}
    sources = [
        {
            "title": source.get("title", ""),
            "url": source.get("url", ""),
        }
        for source in state.get("sources", [])
    ]
    # 工作记忆层：
    working_memory = {
        "node": node,
        "task": state.get("task", ""),
        "session_id": state.get("session_id", ""),
        "session_turn": state.get("session_turn", 0),
        "session_context": _short_text(state.get("session_context", ""), MAX_TEXT_CHARS["session_context"]),
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "research_notes": _short_text(state.get("research_notes", ""), MAX_TEXT_CHARS["research_notes"]),
        "sources": sources,
        "agent_handoffs": _trim_handoffs(state.get("agent_handoffs", [])),
        "code_agent_summary": _short_text(state.get("code_agent_summary", ""), MAX_TEXT_CHARS["code_agent_summary"]),
        "verifier_summary": _short_text(state.get("verifier_summary", ""), MAX_TEXT_CHARS["verifier_summary"]),
        "verification_checks": state.get("verification_checks", []),
        "last_error": _short_text(state.get("last_error", ""), MAX_TEXT_CHARS["last_error"]),
        "attempts": state.get("attempts", 0),
        "max_attempts": state.get("max_attempts", 3),
        "context_next_node": state.get("context_next_node", ""),
    }
    history_summary = state.get("history_summary") or history.get("content", "")
    # 历史记忆总结相关信息：总结信息路径， 是不是存在， 总结内容（压缩后）， 重要信息摘要路径，存在与否， 上下文摘要， 上下文压缩事件
    history_summary_store = {
        "history_path": HISTORY_SUMMARY_FILE,
        "history_exists": history.get("exists", False),
        "history_summary": _short_text(history_summary, MAX_TEXT_CHARS["history_summary"]),
        "notepad_path": NOTEPAD_FILE,
        "notepad_exists": notepad.get("exists", False),
        "notepad": _short_text(notepad.get("content", ""), MAX_TEXT_CHARS["notepad"]),
        "context_summary": _short_text(state.get("context_summary", ""), MAX_TEXT_CHARS["context_summary"]),
        "compression_events": state.get("compression_events", [])[-3:],
    }
    return {
        "rules": dict(RULES_LAYER),
        "working_memory": working_memory,
        "history_summary_store": history_summary_store,
        "topic_index": _build_topic_index(runtime),
    }


def format_layered_memory_for_prompt(memory: dict[str, Any]) -> str:
    """将分层记忆格式化为 JSON 字符串，用于 LLM 提示词

    Args:
        memory: 分层记忆字典

    Returns:
        格式化后的 JSON 字符串
    """
    return json.dumps(memory, ensure_ascii=False, indent=2, default=str)


def memory_event(memory: dict[str, Any], *, node: str) -> dict[str, Any]:
    """生成记忆快照事件，用于实时输出

    Args:
        memory: 分层记忆字典
        node: 当前节点名称

    Returns:
        事件字典，包含各层的摘要信息
    """
    working = memory.get("working_memory", {})
    history = memory.get("history_summary_store", {})
    topic_index = memory.get("topic_index", {})
    return {
        "type": "memory_snapshot",
        "node": node,
        "rules_count": len(memory.get("rules", {}).get("rules", [])),
        "todo_count": len(working.get("todos", [])),
        "source_count": len(working.get("sources", [])),
        "handoff_count": len(working.get("agent_handoffs", [])),
        "notepad_exists": bool(history.get("notepad_exists")),
        "history_exists": bool(history.get("history_exists")),
        "history_path": history.get("history_path", HISTORY_SUMMARY_FILE),
        "topic_count": topic_index.get("topic_count", 0),
        "layers": {
            "rules": _event_layer_summary(memory.get("rules", {})),
            "working_memory": _event_layer_summary(working),
            "history_summary_store": _event_layer_summary(history),
            "topic_index": _event_layer_summary(topic_index),
        },
    }


def read_history_summary(state: RuntimeState) -> dict[str, Any]:
    """读取历史摘要文件

    Args:
        state: 运行时状态

    Returns:
        包含历史摘要内容的字典
    """
    try:
        path = state.assert_workspace_path(state.workspace / HISTORY_SUMMARY_FILE)
    except (ValueError, PathSecurityError) as exc:
        logger.debug("history summary path error: %s", exc)
        return {"ok": False, "path": HISTORY_SUMMARY_FILE, "content": "", "exists": False}
    if not path.exists():
        return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": "", "exists": False}
    try:
        content = read_text_lossy(path)
    except OSError as exc:
        logger.debug("history summary read error: %s", exc)
        return {"ok": False, "path": HISTORY_SUMMARY_FILE, "content": "", "exists": True}
    state.record_read(path, complete=True)
    return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": content, "exists": True}


def persist_history_summary(state: RuntimeState, summary: str) -> dict[str, Any]:
    """持久化历史摘要到文件

    Args:
        state: 运行时状态
        summary: 要保存的摘要内容

    Returns:
        操作结果字典
    """
    try:
        path = state.assert_workspace_path(state.workspace / HISTORY_SUMMARY_FILE)
    except (ValueError, PathSecurityError) as exc:
        logger.debug("history summary path error: %s", exc)
        return {"ok": False, "path": HISTORY_SUMMARY_FILE, "error": str(exc)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"# MokioClaw History Summary\n\n_Updated: {timestamp}_\n\n{summary.strip()}\n"
        path.write_text(content, encoding="utf-8")
        state.record_read(path, complete=True)
    except OSError as exc:
        logger.debug("history summary write error: %s", exc)
        return {"ok": False, "path": HISTORY_SUMMARY_FILE, "error": str(exc)}
    return {"ok": True, "path": HISTORY_SUMMARY_FILE, "lines": len(content.splitlines())}


def _event_layer_summary(layer: dict[str, Any]) -> str:
    if not layer:
        return "(empty)"
    text = json.dumps(layer, ensure_ascii=False, default=str)
    return _short_text(text, 420)


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """截断智能体交接记录，使用 memory 模块的长度限制"""
    return trim_handoffs(
        handoffs,
        instruction_limit=MAX_TEXT_CHARS["agent_handoff_instruction"],
        result_limit=MAX_TEXT_CHARS["agent_handoff_result"],
    )


def _short_text(text: str, limit: int) -> str:
    """截断文本到指定长度"""
    return truncate(text, limit)


def _build_topic_index(runtime: RuntimeState) -> dict[str, Any]:
    """构建主题记忆索引层

    加载 MEMORY.md 索引和主题文件列表，不加载完整主题内容。
    模型需要详细记忆时，自行调用 FileReadTool 读取主题文件。

    Args:
        runtime: 运行时状态

    Returns:
        主题索引字典
    """
    try:
        store = TopicStore(runtime.workspace)
        index_text = store.load_index()
        topics = store.list_topics()
    except Exception as exc:
        logger.debug("topic index build failed: %s", exc)
        return {"index": "", "topics": [], "topic_count": 0}

    return {
        "index": _short_text(index_text, MAX_TEXT_CHARS["history_summary"]),
        "topics": [
            {"name": t.name, "description": t.description, "type": t.topic_type}
            for t in topics
        ],
        "topic_count": len(topics),
    }
