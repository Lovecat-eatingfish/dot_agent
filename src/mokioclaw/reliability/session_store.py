"""Session 存储管理模块

统一管理 session 和轮次级检查点，支持：
- 创建/加载/列出 session
- 保存轮次检查点
- 按轮次回溯
- 按 session 恢复
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import truncate, utc_now
from mokioclaw.reliability.checkpoint import serialize_message, deserialize_messages, snapshot_workspace_git, snapshots_dir
from mokioclaw.state.graph import PersistedState

logger = get_logger(__name__)

# 存储路径常量
SESSIONS_ROOT = Path(".mokioclaw") / "sessions"
SESSION_INDEX_FILE = "index.json"
SESSION_FILE = "session.json"
TURNS_DIR = "turns"

# 默认限制（可配置）
DEFAULT_MAX_SESSIONS = 100
DEFAULT_MAX_TURNS = 200


def sessions_dir(workspace: Path) -> Path:
    """获取 sessions 目录路径"""
    return workspace / SESSIONS_ROOT


def session_index_path(workspace: Path) -> Path:
    """获取 session 索引文件路径"""
    return sessions_dir(workspace) / SESSION_INDEX_FILE


def generate_session_id() -> str:
    """生成 session ID"""
    return f"session-{uuid4().hex[:8]}"


def generate_turn_id(turn: int) -> str:
    """生成轮次 ID"""
    return f"turn-{turn:03d}"


# ============ Session 索引管理 ============


def _load_index(workspace: Path) -> dict[str, Any]:
    """加载 session 索引"""
    path = session_index_path(workspace)
    if not path.exists():
        return {"sessions": [], "latest": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"sessions": [], "latest": None}
    except (OSError, json.JSONDecodeError):
        return {"sessions": [], "latest": None}


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：tmp 文件 + os.replace，避免并发写产生半截 JSON（损坏 index 会连锁清空全部索引）"""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _save_index(workspace: Path, index: dict[str, Any]) -> None:
    """保存 session 索引"""
    _atomic_write_text(session_index_path(workspace), json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def _update_index(workspace: Path, session_id: str, summary: dict[str, Any]) -> None:
    """更新 session 索引"""
    index = _load_index(workspace)
    sessions = index.get("sessions", [])

    # 查找并更新，或新增
    found = False
    for i, s in enumerate(sessions):
        if s.get("session_id") == session_id:
            sessions[i] = {**s, **summary}
            found = True
            break

    if not found:
        sessions.append(summary)

    # 按 updated_at 降序排列
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)

    index["sessions"] = sessions
    index["latest"] = session_id
    _save_index(workspace, index)


# ============ Session CRUD ============


def create_session(workspace: Path, task: str = "", session_id: str | None = None) -> dict[str, Any]:
    """创建新 session

    Args:
        workspace: 工作区路径
        task: 任务描述
        session_id: 可选的 session ID（用于恢复）

    Returns:
        session 数据字典
    """
    sid = session_id or generate_session_id()
    now = utc_now()

    session = {
        "session_id": sid,
        "workspace": str(workspace),
        "created_at": now,
        "updated_at": now,
        "turn_index": 0,
        "task": task,
        "messages": [],
        "turns": [],
        "latest_checkpoint": None,
        "status": "running",
    }

    # 创建 session 目录
    session_path = _session_dir(workspace, sid)
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / TURNS_DIR).mkdir(exist_ok=True)

    # 保存 session.json
    _save_session_file(workspace, sid, session)

    # 更新索引
    _update_index(workspace, sid, {
        "session_id": sid,
        "task": task[:200] if task else "",
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "turn_index": 0,
    })

    logger.info("created session %s", sid)
    return session


def load_session(workspace: Path, session_id: str) -> dict[str, Any] | None:
    """加载指定 session

    Args:
        workspace: 工作区路径
        session_id: session ID

    Returns:
        session 数据字典，不存在返回 None
    """
    path = _session_file_path(workspace, session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def get_latest_session(workspace: Path) -> dict[str, Any] | None:
    """获取最新 session

    Args:
        workspace: 工作区路径

    Returns:
        最新的 session 数据字典，无 session 返回 None
    """
    index = _load_index(workspace)
    latest_id = index.get("latest")
    if not latest_id:
        # 尝试从 sessions 列表获取
        sessions = index.get("sessions", [])
        if sessions:
            latest_id = sessions[0].get("session_id")

    if not latest_id:
        return None

    return load_session(workspace, latest_id)


def list_sessions(workspace: Path, limit: int = 50) -> list[dict[str, Any]]:
    """列出所有 session

    Args:
        workspace: 工作区路径
        limit: 最大返回数量

    Returns:
        session 摘要列表，按更新时间降序
    """
    index = _load_index(workspace)
    sessions = index.get("sessions", [])
    return sessions[:limit]


def save_session(workspace: Path, session: dict[str, Any]) -> None:
    """保存 session 数据

    Args:
        workspace: 工作区路径
        session: session 数据字典
    """
    session_id = session.get("session_id")
    if not session_id:
        return

    session["updated_at"] = utc_now()
    _save_session_file(workspace, session_id, session)

    # 更新索引
    _update_index(workspace, session_id, {
        "session_id": session_id,
        "task": session.get("task", "")[:200],
        "created_at": session.get("created_at", ""),
        "updated_at": session["updated_at"],
        "status": session.get("status", "running"),
        "turn_index": session.get("turn_index", 0),
    })


# ============ 轮次管理 ============


def append_user_turn(workspace: Path, session: dict[str, Any], content: str) -> int:
    """添加用户轮次

    Args:
        workspace: 工作区路径
        session: session 数据字典
        content: 用户输入内容

    Returns:
        轮次号
    """
    turn = int(session.get("turn_index", 0)) + 1
    session["turn_index"] = turn
    session["task"] = content[:500]  # 更新当前任务

    turn_data = {
        "turn": turn,
        "role": "user",
        "content": content,
        "timestamp": utc_now(),
    }

    turns = session.get("turns", [])
    turns.append(turn_data)
    session["turns"] = turns

    save_session(workspace, session)
    return turn


def append_assistant_turn(
    workspace: Path,
    session: dict[str, Any],
    turn: int,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    summary: str = "",
    state_summary: PersistedState | None = None,
) -> None:
    """添加 assistant 轮次

    Args:
        workspace: 工作区路径
        session: session 数据字典
        turn: 轮次号
        content: assistant 回复内容
        tool_calls: 工具调用列表
        summary: 摘要
    """
    turn_data = {
        "turn": turn,
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "summary": summary or content[:500],
        "state_summary": _build_state_summary(state_summary or {}) if state_summary else {},
        "timestamp": utc_now(),
    }

    turns = session.get("turns", [])
    turns.append(turn_data)
    session["turns"] = turns
    if state_summary:
        session["last_state_summary"] = _build_state_summary(state_summary)

    save_session(workspace, session)


# ============ 轮次检查点 ============


def save_turn_checkpoint(
    workspace: Path,
    session: dict[str, Any],
    turn: int,
    task: str,
    state: dict[str, Any] | None = None,
    turn_messages: list[Any] | None = None,
) -> dict[str, Any]:
    """保存轮次检查点

    Args:
        workspace: 工作区路径
        session: session 数据字典
        turn: 轮次号
        task: 本轮任务
        state: 可选的状态快照
        turn_messages: 本轮的 messages 列表（用于 rewind）

    Returns:
        检查点数据
    """
    session_id = session.get("session_id")
    turn_id = generate_turn_id(turn)
    now = utc_now()

    # 先 Git commit 用户项目代码，获取 commit hash
    git_commit = _snapshot_git(workspace, session_id)

    # 序列化本轮 messages
    serialized_messages = [serialize_message(m) for m in (turn_messages or [])]

    # 构建检查点数据
    checkpoint = {
        "turn": turn,
        "turn_id": turn_id,
        "timestamp": now,
        "session_id": session_id,
        "task": task,
        "git_commit_id": git_commit,
        "trace_id": getattr(state.get("runtime") if state else None, "trace_id", None) if state else None,
        "state_summary": _build_state_summary(state) if state else None,
        "messages": serialized_messages,
    }

    # 保存检查点文件
    checkpoint_path = _turn_file_path(workspace, session_id, turn_id)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8"
    )

    # 更新 session 的 latest_checkpoint
    session["latest_checkpoint"] = turn_id
    save_session(workspace, session)

    logger.info("saved checkpoint for session %s turn %d (git=%s)", session_id, turn, git_commit or "none")
    return checkpoint


def load_turn_checkpoint(
    workspace: Path,
    session_id: str,
    turn: int,
) -> dict[str, Any] | None:
    """加载轮次检查点

    Args:
        workspace: 工作区路径
        session_id: session ID
        turn: 轮次号

    Returns:
        检查点数据，不存在返回 None
    """
    turn_id = generate_turn_id(turn)
    path = _turn_file_path(workspace, session_id, turn_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_turn_checkpoints(workspace: Path, session_id: str) -> list[dict[str, Any]]:
    """列出 session 的所有轮次检查点

    Args:
        workspace: 工作区路径
        session_id: session ID

    Returns:
        检查点列表，按轮次号升序
    """
    checkpoints_dir = _turns_dir(workspace, session_id)
    if not checkpoints_dir.exists():
        return []

    checkpoints = []
    for path in sorted(checkpoints_dir.glob("turn-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                checkpoints.append(data)
        except (OSError, json.JSONDecodeError):
            continue

    checkpoints.sort(key=lambda c: c.get("turn", 0))
    return checkpoints


def rollback_to_turn(
    workspace: Path,
    session_id: str,
    turn: int,
    restore_files: bool = True,
) -> dict[str, Any] | None:
    """回滚到指定轮次

    Args:
        workspace: 工作区路径
        session_id: session ID
        turn: 目标轮次号
        restore_files: 是否恢复工作区文件

    Returns:
        目标轮次的检查点数据，失败返回 None
    """
    checkpoint = load_turn_checkpoint(workspace, session_id, turn)
    if not checkpoint:
        logger.error("checkpoint not found for session %s turn %d", session_id, turn)
        return None

    # 恢复工作区文件
    if restore_files:
        git_commit = checkpoint.get("git_commit_id")
        if git_commit:
            success = _restore_git(workspace, session_id, git_commit)
            if not success:
                logger.warning("git restore failed for session %s", session_id)

    # 更新 session：删除目标轮次之后的所有轮次
    session = load_session(workspace, session_id)
    if session:
        turns = session.get("turns", [])
        session["turns"] = [t for t in turns if t.get("turn", 0) <= turn]
        # 截断 messages 到目标轮次：重新加载 0→turn 的 messages
        session["messages"] = _serialize_message_list(load_turns_up_to(workspace, session_id, turn))
        session["turn_index"] = turn
        session["latest_checkpoint"] = generate_turn_id(turn)
        save_session(workspace, session)

    logger.info("rolled back session %s to turn %d", session_id, turn)
    return checkpoint


# ============ Messages 持久化 ============


def append_messages_to_session(
    workspace: Path,
    session: dict[str, Any],
    messages: list[Any],
) -> None:
    """将本轮新增 messages append 到 session.json 的 messages[] 中

    Args:
        workspace: 工作区路径
        session: session 数据字典
        messages: 本轮新增的消息列表（BaseMessage 对象）
    """
    existing = session.get("messages", [])
    serialized = existing + [serialize_message(m) for m in messages]
    session["messages"] = serialized
    save_session(workspace, session)


def load_turns_up_to(workspace: Path, session_id: str, target_turn: int) -> list[BaseMessage]:
    """加载 turn-0 到 target_turn 的所有 messages，返回完整 message list

    用于 rewind：按顺序加载每个 turn 的 messages，拼回全局 message list。

    Args:
        workspace: 工作区路径
        session_id: session ID
        target_turn: 目标轮次号

    Returns:
        累积的 BaseMessage 列表
    """
    all_messages: list[BaseMessage] = []
    for n in range(1, target_turn + 1):
        turn_id = generate_turn_id(n)
        turn_file = _turn_file_path(workspace, session_id, turn_id)
        if not turn_file.exists():
            continue
        try:
            data = json.loads(turn_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_msgs = data.get("messages", [])
                all_messages.extend(deserialize_messages(raw_msgs))
        except (OSError, json.JSONDecodeError):
            continue
    return all_messages


def load_session_messages(workspace: Path, session_id: str) -> list[BaseMessage]:
    """加载 session 的完整 messages[] 用于 resume

    Args:
        workspace: 工作区路径
        session_id: session ID

    Returns:
        完整的 BaseMessage 列表
    """
    session = load_session(workspace, session_id)
    if not session:
        return []
    raw = session.get("messages", [])
    return deserialize_messages(raw)


def _serialize_message_list(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """将 BaseMessage 列表序列化为 JSON 可存储的 dict 列表"""
    return [serialize_message(m) for m in messages]


# ============ Session 状态管理 ============


def finish_session(workspace: Path, session_id: str) -> None:
    """标记 session 完成"""
    session = load_session(workspace, session_id)
    if session:
        session["status"] = "finished"
        save_session(workspace, session)


def interrupt_session(workspace: Path, session_id: str) -> None:
    """标记 session 中断"""
    session = load_session(workspace, session_id)
    if session:
        session["status"] = "interrupted"
        save_session(workspace, session)


def fork_session(workspace: Path, source_session_id: str, *, task: str = "") -> dict[str, Any] | None:
    """从已有 session 创建分支

    复制源 session 的 turns 和 checkpoint，生成新的 session ID。
    新 session 状态为 running，可以独立继续。

    Args:
        workspace: 工作区路径
        source_session_id: 源 session ID
        task: 可选的新任务描述，默认继承源 session

    Returns:
        新 session 数据字典，源不存在返回 None
    """
    source = load_session(workspace, source_session_id)
    if not source:
        logger.error("fork source session not found: %s", source_session_id)
        return None

    new_sid = generate_session_id()
    now = utc_now()
    inherited_task = task or source.get("task", "")

    forked = {
        "session_id": new_sid,
        "workspace": str(workspace),
        "created_at": now,
        "updated_at": now,
        "turn_index": source.get("turn_index", 0),
        "task": inherited_task,
        "messages": list(source.get("messages", [])),
        "turns": list(source.get("turns", [])),
        "latest_checkpoint": source.get("latest_checkpoint"),
        "status": "running",
        "forked_from": source_session_id,
    }

    session_path = _session_dir(workspace, new_sid)
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / TURNS_DIR).mkdir(exist_ok=True)

    source_turns_dir = _turns_dir(workspace, source_session_id)
    if source_turns_dir.exists():
        import shutil
        target_turns_dir = _turns_dir(workspace, new_sid)
        for ckpt_file in source_turns_dir.glob("*.json"):
            shutil.copy2(ckpt_file, target_turns_dir / ckpt_file.name)

    _save_session_file(workspace, new_sid, forked)

    _update_index(workspace, new_sid, {
        "session_id": new_sid,
        "task": inherited_task[:200] if inherited_task else "",
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "turn_index": forked["turn_index"],
    })

    logger.info("forked session %s from %s", new_sid, source_session_id)
    return forked


# ============ 内部辅助函数 ============


def _session_dir(workspace: Path, session_id: str) -> Path:
    """获取 session 目录路径"""
    return sessions_dir(workspace) / session_id


def _session_file_path(workspace: Path, session_id: str) -> Path:
    """获取 session.json 文件路径"""
    return _session_dir(workspace, session_id) / SESSION_FILE


def _turns_dir(workspace: Path, session_id: str) -> Path:
    """获取轮次目录路径"""
    return _session_dir(workspace, session_id) / TURNS_DIR


def _turn_file_path(workspace: Path, session_id: str, turn_id: str) -> Path:
    """获取轮次检查点文件路径"""
    return _turns_dir(workspace, session_id) / f"{turn_id}.json"


def _save_session_file(workspace: Path, session_id: str, session: dict[str, Any]) -> None:
    """保存 session.json 文件"""
    _atomic_write_text(
        _session_file_path(workspace, session_id),
        json.dumps(session, ensure_ascii=False, indent=2, default=str) + "\n",
    )


def build_resume_context(session: dict[str, Any], *, max_chars: int = 5000) -> str:
    """Build actionable context for a resumed coding turn."""
    last_state = session.get("last_state_summary") or {}
    recent_turns = session.get("turns", [])[-6:]
    payload = {
        "session_id": session.get("session_id", ""),
        "status": session.get("status", ""),
        "turn_index": session.get("turn_index", 0),
        "messages_count": len(session.get("messages", [])),
        "task": truncate(str(session.get("task", "")), 600),
        "latest_checkpoint": session.get("latest_checkpoint"),
        "resume_goal": _resume_goal(session),
        "resume_summary": _resume_summary(session, last_state),
        "continuation_hint": _continuation_hint(session, last_state),
        "last_state_summary": last_state,
        "recent_turns": [
            {
                "turn": turn.get("turn"),
                "role": turn.get("role", ""),
                "summary": truncate(str(turn.get("summary") or turn.get("content", "")), 500),
                "state_summary": turn.get("state_summary", {}),
            }
            for turn in recent_turns
        ],
    }
    return truncate(json.dumps(payload, ensure_ascii=False, indent=2, default=str), max_chars)


def _resume_goal(session: dict[str, Any]) -> str:
    task = str(session.get("task", "")).strip()
    if not task:
        return "(no task recorded)"
    return truncate(task, 500)


def _resume_summary(session: dict[str, Any], last_state: dict[str, Any]) -> str:
    parts = []
    if session.get("status"):
        parts.append(f"status={session.get('status')}")
    if session.get("latest_checkpoint"):
        parts.append(f"checkpoint={session.get('latest_checkpoint')}")
    if last_state.get("attempts") is not None:
        parts.append(f"attempts={last_state.get('attempts')}")
    if last_state.get("plan_summary"):
        parts.append(f"plan={truncate(str(last_state.get('plan_summary', '')), 300)}")
    if last_state.get("verifier_summary"):
        parts.append(f"verifier={truncate(str(last_state.get('verifier_summary', '')), 300)}")
    if last_state.get("repair_instruction"):
        parts.append(f"repair={truncate(str(last_state.get('repair_instruction', '')), 300)}")
    if last_state.get("passed") is not None:
        parts.append(f"passed={last_state.get('passed')}")
    return " | ".join(parts)


def _continuation_hint(session: dict[str, Any], last_state: dict[str, Any]) -> str:
    if last_state.get("passed") is True:
        return "Continue from the last successful state and verify remaining follow-up tasks."
    if last_state.get("repair_instruction"):
        return f"Resume the repair loop from: {truncate(str(last_state.get('repair_instruction')), 400)}"
    if last_state.get("plan_summary"):
        return f"Continue the current plan: {truncate(str(last_state.get('plan_summary')), 400)}"
    if session.get("task"):
        return f"Continue the original task: {truncate(str(session.get('task')), 400)}"
    return "Continue the current session without restarting the thread."


def _build_state_summary(state: dict[str, Any]) -> PersistedState:
    """构建状态摘要（PersistedState：仅含跨轮恢复所需的字段）"""
    checks = state.get("verification_checks", []) or []
    compression = state.get("compression_events", []) or []
    handoffs = state.get("agent_handoffs", []) or []
    handoff_str = "; ".join(
        f"{h.get('from_agent','')}->{h.get('to_agent','')}: {truncate(str(h.get('instruction','')), 120)}"
        for h in handoffs[:5]
    ) if isinstance(handoffs, list) else str(handoffs)

    return {
        "plan_summary": truncate(str(state.get("plan_summary", "")), 1000),
        "todos": state.get("todos", [])[:20],
        "acceptance_criteria": state.get("acceptance_criteria", [])[:20],
        "verification_commands": state.get("verification_commands", [])[:20],
        "verification_checks": checks[:20],
        "passed": state.get("passed"),
        "attempts": state.get("attempts", 0),
        "verifier_summary": truncate(str(state.get("verifier_summary", "")), 1000),
        "repair_instruction": truncate(str(state.get("repair_instruction", "")), 1000),
        "last_error": truncate(str(state.get("last_error", "")), 1000),
        "trace_id": getattr(state.get("runtime"), "trace_id", None) if state.get("runtime") is not None else state.get("trace_id"),
        "messages_count": len(state.get("messages", [])),
        # 新增：上下文压缩
        "context_summary": truncate(str(state.get("context_summary", "")), 1000),
        "history_summary": truncate(str(state.get("history_summary", "")), 1000),
        "compression_events": compression[:10],
        # 新增：智能体交互
        "research_notes": truncate(str(state.get("research_notes", "")), 1000),
        "sources": state.get("sources", [])[:10],
        "agent_handoffs": handoff_str,
        "code_agent_summary": truncate(str(state.get("code_agent_summary", "") or state.get("last_actor_summary", "")), 1000),
        "search_agent_summary": truncate(str(state.get("search_agent_summary", "")), 1000),
        # 新增：校验结果
        "verification_results": state.get("verification_results", [])[:20],
    }


def _snapshot_git(workspace: Path, session_id: str) -> str | None:
    """Git 快照工作区（使用共享仓库）"""
    shared_git_dir = snapshots_dir(workspace)
    git_commit, _ = snapshot_workspace_git(workspace, shared_git_dir.parent)
    return git_commit


def _restore_git(workspace: Path, session_id: str, commit: str) -> bool:
    """从 Git 快照恢复工作区（使用共享仓库）

    Args:
        workspace: 工作区路径
        session_id: session ID
        commit: 目标 commit hash

    Returns:
        是否成功
    """
    import shutil as sh

    if sh.which("git") is None:
        return False

    shared_git_dir = snapshots_dir(workspace)
    if not shared_git_dir.exists():
        return False

    try:
        from mokioclaw.reliability.checkpoint import _git
        _git(workspace, shared_git_dir, ["checkout", "-f", commit, "--", "."])
        return True
    except Exception as exc:
        logger.warning("git restore failed: %s", exc)
        return False
