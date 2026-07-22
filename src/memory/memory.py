from typing import Any

from core import RuntimeState
from tools.file_tool import read_text_lossy
from tools.notepad_tool import read_notepad

HISTORY_SUMMARY_FILE = "HISTORY_SUMMARY.md"

RULES_LAYER = {
    "scope": "workspace",
    "storage": "internal",
    "rules": [
        "仅在当前工作区内工作。",
        "使用相对于工作区的路径；不要为路径添加 workspace/ 前缀。",
        "尽可能将持久的任务上下文放在原始消息记录之外。",
        "将 TODO.md 视为工作计划状态，NOTEPAD.md 视为持久笔记，HISTORY_SUMMARY.md 视为压缩的历史记录。",
        "不要向代理暴露内存写入工具；分层内存由运行时组装。",
    ],
}


def build_layered_memory(state: dict[str, Any], *, node: str = "graph") -> dict[str, Any]:
    runtime = state["runtime"]
    # 读取重要持久笔记
    notepad = read_notepad(runtime)
    history = read_history_summary(runtime)

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
    }


# 读取历史摘要总结文件
def read_history_summary(state: RuntimeState) -> dict[str, Any]:
    path = state.assert_workspace_path(state.workspace / HISTORY_SUMMARY_FILE)
    if not path.exists():
        return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": "", "exists": False}
    content = read_text_lossy(path)
    state.record_read(path, complete=True)
    return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": content, "exists": True}
