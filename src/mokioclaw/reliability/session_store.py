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
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import truncate, utc_now

logger = get_logger(__name__)

# 存储路径常量
SESSIONS_ROOT = Path(".mokioclaw") / "sessions"
SESSION_INDEX_FILE = "index.json"
SESSION_FILE = "session.json"
CHECKPOINTS_DIR = "checkpoints"
GIT_DIR = "git"

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


def _save_index(workspace: Path, index: dict[str, Any]) -> None:
    """保存 session 索引"""
    path = session_index_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        "turns": [],
        "latest_checkpoint": None,
        "status": "running",
    }

    # 创建 session 目录
    session_path = _session_dir(workspace, sid)
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / CHECKPOINTS_DIR).mkdir(exist_ok=True)

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
    state_summary: dict[str, Any] | None = None,
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
) -> dict[str, Any]:
    """保存轮次检查点

    Args:
        workspace: 工作区路径
        session: session 数据字典
        turn: 轮次号
        task: 本轮任务
        state: 可选的状态快照

    Returns:
        检查点数据
    """
    session_id = session.get("session_id")
    turn_id = generate_turn_id(turn)
    now = utc_now()

    # Git 快照
    git_commit = _snapshot_git(workspace, session_id)

    # 构建检查点数据
    checkpoint = {
        "turn": turn,
        "turn_id": turn_id,
        "timestamp": now,
        "session_id": session_id,
        "task": task,
        "git_commit": git_commit,
        "state_summary": _build_state_summary(state) if state else None,
    }

    # 保存检查点文件
    checkpoint_path = _checkpoint_file_path(workspace, session_id, turn_id)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    # 更新 session 的 latest_checkpoint
    session["latest_checkpoint"] = turn_id
    save_session(workspace, session)

    logger.info("saved checkpoint for session %s turn %d", session_id, turn)
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
    path = _checkpoint_file_path(workspace, session_id, turn_id)
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
    checkpoints_dir = _checkpoints_dir(workspace, session_id)
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
        git_commit = checkpoint.get("git_commit")
        if git_commit:
            success = _restore_git(workspace, session_id, git_commit)
            if not success:
                logger.warning("git restore failed for session %s", session_id)

    # 更新 session：删除目标轮次之后的所有轮次
    session = load_session(workspace, session_id)
    if session:
        turns = session.get("turns", [])
        session["turns"] = [t for t in turns if t.get("turn", 0) <= turn]
        session["turn_index"] = turn
        session["latest_checkpoint"] = generate_turn_id(turn)
        save_session(workspace, session)

    logger.info("rolled back session %s to turn %d", session_id, turn)
    return checkpoint


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
        "turns": list(source.get("turns", [])),
        "latest_checkpoint": source.get("latest_checkpoint"),
        "status": "running",
        "forked_from": source_session_id,
    }

    session_path = _session_dir(workspace, new_sid)
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / CHECKPOINTS_DIR).mkdir(exist_ok=True)

    source_ckpts_dir = _checkpoints_dir(workspace, source_session_id)
    if source_ckpts_dir.exists():
        import shutil
        target_ckpts_dir = _checkpoints_dir(workspace, new_sid)
        for ckpt_file in source_ckpts_dir.glob("*.json"):
            shutil.copy2(ckpt_file, target_ckpts_dir / ckpt_file.name)

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


def _checkpoints_dir(workspace: Path, session_id: str) -> Path:
    """获取检查点目录路径"""
    return _session_dir(workspace, session_id) / CHECKPOINTS_DIR


def _checkpoint_file_path(workspace: Path, session_id: str, turn_id: str) -> Path:
    """获取检查点文件路径"""
    return _checkpoints_dir(workspace, session_id) / f"{turn_id}.json"


def _git_dir(workspace: Path, session_id: str) -> Path:
    """获取 Git 目录路径"""
    return _session_dir(workspace, session_id) / GIT_DIR


def _save_session_file(workspace: Path, session_id: str, session: dict[str, Any]) -> None:
    """保存 session.json 文件"""
    path = _session_file_path(workspace, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def build_resume_context(session: dict[str, Any], *, max_chars: int = 5000) -> str:
    """Build actionable context for a resumed coding turn."""
    last_state = session.get("last_state_summary") or {}
    recent_turns = session.get("turns", [])[-6:]
    payload = {
        "session_id": session.get("session_id", ""),
        "status": session.get("status", ""),
        "turn_index": session.get("turn_index", 0),
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


def _build_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """构建状态摘要"""
    checks = state.get("verification_checks", []) or []
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
    }


def _snapshot_git(workspace: Path, session_id: str) -> str | None:
    """Git 快照工作区

    Args:
        workspace: 工作区路径
        session_id: session ID

    Returns:
        Git commit hash，失败返回 None
    """
    import shutil as sh

    if sh.which("git") is None:
        logger.debug("git not found, skipping snapshot")
        return None

    git_dir = _git_dir(workspace, session_id)
    git_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 初始化 git 仓库（如果不存在）
        _git_run(workspace, git_dir, ["init", "-q"])
        _git_run(workspace, git_dir, ["config", "user.name", "MokioClaw"])
        _git_run(workspace, git_dir, ["config", "user.email", "mokioclaw@local"])

        # 配置忽略规则
        _ensure_git_excludes(git_dir)

        # 添加所有文件
        _git_run(workspace, git_dir, ["add", "-A", "--", "."])

        # 检查是否有变更
        status = _git_run(workspace, git_dir, ["status", "--porcelain"]).stdout.strip()
        head = _git_head(workspace, git_dir)

        if not status and head:
            return head

        # 创建 commit
        args = ["commit", "-q", "-m", f"turn checkpoint {utc_now()}"]
        if not status:
            args.append("--allow-empty")
        _git_run(workspace, git_dir, args)

        return _git_head(workspace, git_dir)

    except Exception as exc:
        logger.warning("git snapshot failed: %s", exc)
        return _git_head(workspace, git_dir)


def _restore_git(workspace: Path, session_id: str, commit: str) -> bool:
    """从 Git 快照恢复工作区

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

    git_dir = _git_dir(workspace, session_id)
    if not git_dir.exists():
        return False

    try:
        _git_run(workspace, git_dir, ["checkout", "-f", commit, "--", "."])
        return True
    except Exception as exc:
        logger.warning("git restore failed: %s", exc)
        return False


def _git_run(workspace: Path, git_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行 git 命令"""
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={workspace}", *args],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    )


def _git_head(workspace: Path, git_dir: Path) -> str | None:
    """获取当前 HEAD commit hash"""
    try:
        result = _git_run(workspace, git_dir, ["rev-parse", "--short", "HEAD"])
        return result.stdout.strip() or None
    except Exception:
        return None


def _ensure_git_excludes(git_dir: Path) -> None:
    """确保 git 忽略规则配置"""
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""

    patterns = [
        ".mokioclaw/",
        ".venv/",
        "venv/",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
        ".git/",
    ]

    with exclude_path.open("a", encoding="utf-8") as f:
        for pattern in patterns:
            if pattern not in existing:
                f.write(pattern + "\n")
