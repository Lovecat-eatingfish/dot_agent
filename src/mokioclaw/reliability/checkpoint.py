from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, messages_from_dict, message_to_dict

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import json_safe, truncate_json, utc_now, write_json

logger = get_logger(__name__)

VALID_CHECKPOINT_MODES = {"light", "strict", "off"}
EXECUTIONS_ROOT = Path(".mokioclaw") / "executions"
EXECUTION_FILE = "execution.json"
EVENTS_FILE = "events.jsonl"
STATE_FILE = "state.json"
RECOVERY_FILE = "RECOVERY.md"
SNAPSHOTS_DIR = Path(".mokioclaw") / "snapshots"
GIT_SHARED_REPO = "shared.git"
GIT_COMMIT_PREFIX = "checkpoint"
MAX_RECOVERY_TEXT = 6000
MAX_MANIFEST_ITEMS = 160

# 最大保留的 checkpoint 目录数（每个 workspace）
MAX_CHECKPOINT_DIRS = 5

# 事件文件写入锁（进程内线程安全）
_events_lock = threading.Lock()


def normalize_checkpoint_mode(mode: str | None) -> str:
    normalized = (mode or "light").strip().lower()
    return normalized if normalized in VALID_CHECKPOINT_MODES else "light"


def execution_dir(runtime: Any) -> Path:
    """返回该 workflow 执行的目录路径（exec-{timestamp}-{short_id}）"""
    exec_id = f"exec-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    return runtime.workspace / EXECUTIONS_ROOT / exec_id


def snapshots_dir(workspace: Path) -> Path:
    """返回该 workspace 的共享 git 快照仓库路径"""
    return workspace / SNAPSHOTS_DIR / GIT_SHARED_REPO


def _latest_execution_dir(workspace: Path) -> Path | None:
    """返回最新的 execution 目录（按修改时间排序），不存在返回 None"""
    root = workspace / EXECUTIONS_ROOT
    if not root.exists():
        return None
    dirs = [d for d in root.iterdir() if d.is_dir() and d.name.startswith("exec-")]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


class CheckpointManager:
    def __init__(self, runtime: Any, task: str = "") -> None:
        self.runtime = runtime
        self.workspace = runtime.workspace
        self.mode = normalize_checkpoint_mode(getattr(runtime, "checkpoint_mode", "light"))
        self.task = task
        self.root = execution_dir(self.runtime)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def save(
        self,
        state: dict[str, Any],
        *,
        status: str = "running",
        latest_node: str | None = None,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        self.root.mkdir(parents=True, exist_ok=True)
        if event is not None and self.mode == "strict":
            self._append_event(event)

        if self.mode == "strict":
            _write_json(self.root / STATE_FILE, serialize_state(state))

        manifest = workspace_manifest(self.workspace)
        git_commit, git_error = snapshot_workspace_git(self.workspace, snapshots_dir(self.workspace).parent)
        payload = self._payload(state, status=status, latest_node=latest_node, manifest=manifest, git_commit=git_commit, git_error=git_error)
        _write_json(self.root / EXECUTION_FILE, payload)
        (self.root / RECOVERY_FILE).write_text(build_recovery_markdown(payload), encoding="utf-8")

        # 保存成功后清理旧 checkpoint（异步安全，失败不影响主流程）
        try:
            self.cleanup_old_checkpoints()
        except Exception as exc:
            logger.debug("checkpoint cleanup skipped: %s", exc)

        return checkpoint_saved_event(payload)

    def cleanup_old_checkpoints(self, *, max_dirs: int = MAX_CHECKPOINT_DIRS) -> int:
        """清理旧的 execution 目录，只保留最近的 max_dirs 个

        Returns:
            删除的目录数量
        """
        parent = self.root.parent
        if not parent.exists():
            return 0

        # 收集所有 exec-* 目录
        dirs = sorted(
            [d for d in parent.iterdir() if d.is_dir() and d.name.startswith("exec-")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for old_dir in dirs[max_dirs:]:
            try:
                shutil.rmtree(old_dir)
                removed += 1
            except OSError as exc:
                logger.warning("failed to remove old checkpoint %s: %s", old_dir, exc)
        return removed

    def _append_event(self, event: dict[str, Any]) -> None:
        line = {
            "timestamp": utc_now(),
            "event": json_safe(event),
        }
        with _events_lock:
            try:
                with (self.root / EVENTS_FILE).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                logger.warning("failed to append checkpoint event: %s", exc)

    def _payload(
        self,
        state: dict[str, Any],
        *,
        status: str,
        latest_node: str | None,
        manifest: list[dict[str, Any]],
        git_commit: str | None,
        git_error: str | None,
    ) -> dict[str, Any]:
        task = str(state.get("task") or self.task or "")
        summary = state_summary(state)
        return {
            "version": 1,
            "updated_at": utc_now(),
            "mode": self.mode,
            "status": status,
            "workspace": str(self.workspace),
            "execution_dir": str(self.root),
            "execution_file": str(self.root / EXECUTION_FILE),
            "recovery_file": str(self.root / RECOVERY_FILE),
            "state_file": str(self.root / STATE_FILE) if self.mode == "strict" else "",
            "events_file": str(self.root / EVENTS_FILE) if self.mode == "strict" else "",
            "task": task,
            "latest_node": latest_node or "",
            "next_node": state.get("context_next_node", ""),
            "attempts": state.get("attempts", 0),
            "max_attempts": state.get("max_attempts", 0),
            "summary": summary,
            "workspace_manifest": manifest,
            "git": {
                "dir": str(snapshots_dir(self.workspace)),
                "commit": git_commit,
                "error": git_error,
            },
            "resume_command": resume_command(self.workspace),
        }


def checkpoint_saved_event(payload: dict[str, Any]) -> dict[str, Any]:
    git_info = payload.get("git") or {}
    return {
        "type": "checkpoint_saved",
        "mode": payload.get("mode", ""),
        "status": payload.get("status", ""),
        "workspace": payload.get("workspace", ""),
        "path": payload.get("execution_dir", ""),
        "execution_file": payload.get("execution_file", ""),
        "recovery_file": payload.get("recovery_file", ""),
        "git_commit": git_info.get("commit"),
        "git_error": git_info.get("error"),
        "resume_command": payload.get("resume_command", ""),
    }


def checkpoint_resumed_event(
    *,
    workspace: Path,
    mode: str,
    source: str,
    fallback: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    exec_root = EXECUTIONS_ROOT  # 路径信息已在 source 参数中传递
    return {
        "type": "checkpoint_resumed",
        "mode": mode,
        "workspace": str(workspace),
        "path": str(workspace / exec_root),
        "source": source,
        "fallback": fallback,
        "reason": reason,
    }


def load_resume_inputs(
    runtime: Any,
    *,
    task: str | None = None,
    max_attempts: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requested_mode = normalize_checkpoint_mode(getattr(runtime, "checkpoint_mode", "light"))
    if requested_mode == "strict":
        try:
            state = load_strict_state(runtime, max_attempts=max_attempts)
        except Exception as exc:
            inputs = build_light_resume_inputs(runtime, task=task, max_attempts=max_attempts)
            latest = _latest_execution_dir(runtime.workspace)
            source = str(latest / RECOVERY_FILE) if latest else ""
            event = checkpoint_resumed_event(
                workspace=runtime.workspace,
                mode="light",
                source=source,
                fallback=True,
                reason=f"strict resume unavailable: {type(exc).__name__}: {exc}",
            )
            return inputs, event
        latest = _latest_execution_dir(runtime.workspace)
        source = str(latest / STATE_FILE) if latest else ""
        event = checkpoint_resumed_event(
            workspace=runtime.workspace,
            mode="strict",
            source=source,
        )
        return state, event

    inputs = build_light_resume_inputs(runtime, task=task, max_attempts=max_attempts)
    latest = _latest_execution_dir(runtime.workspace)
    source = str(latest / RECOVERY_FILE) if latest else ""
    event = checkpoint_resumed_event(
        workspace=runtime.workspace,
        mode="light",
        source=source,
    )
    return inputs, event


def load_strict_state(runtime: Any, *, max_attempts: int = 3) -> dict[str, Any]:
    latest = _latest_execution_dir(runtime.workspace)
    if latest is None:
        raise FileNotFoundError("no execution directory found")
    state_path = latest / STATE_FILE
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("state.json is not an object")
    state = deserialize_state(raw, runtime)
    state["max_attempts"] = max_attempts
    metadata = dict(state.get("metadata", {}))
    metadata.update(
        {
            "resumed": True,
            "resume_mode": "strict",
            "resume_workspace": str(runtime.workspace),
        }
    )
    state["metadata"] = metadata
    return state


def build_light_resume_inputs(runtime: Any, *, task: str | None = None, max_attempts: int = 3) -> dict[str, Any]:
    checkpoint = read_checkpoint(runtime.workspace)
    recovery = read_checkpoint_text(runtime.workspace, RECOVERY_FILE)
    # 内部工作文件已收进 .mokioclaw/，读取时回退兼容旧版根目录文件
    todo = read_workspace_text_any(runtime.workspace, ".mokioclaw/TODO.md", "TODO.md")
    history = read_workspace_text_any(runtime.workspace, ".mokioclaw/HISTORY_SUMMARY.md", "HISTORY_SUMMARY.md")

    original_task = str(checkpoint.get("task") or "").strip()
    resume_task = normalize_resume_task(task.strip() if isinstance(task, str) and task.strip() else original_task)
    if not resume_task:
        resume_task = "Continue the interrupted MokioClaw task from the checkpoint."
    else:
        resume_task = f"Continue this MokioClaw task from the checkpoint: {resume_task}"

    context_parts = [
        "# Checkpoint Recovery Context",
        recovery,
        "## TODO.md",
        todo,
        "## HISTORY_SUMMARY.md",
        history,
    ]
    context_summary = trim_text("\n\n".join(part for part in context_parts if part), MAX_RECOVERY_TEXT)
    summary = checkpoint.get("summary") if isinstance(checkpoint.get("summary"), dict) else {}

    inputs: dict[str, Any] = {
        "task": resume_task,
        "runtime": runtime,
        "messages": [],
        "attempts": 0,
        "max_attempts": max_attempts,
        "context_summary": context_summary,
        "history_summary": trim_text(history, 2400),
        "metadata": {
            "resumed": True,
            "resume_mode": "light",
            "resume_workspace": str(runtime.workspace),
            "original_task": original_task,
        },
    }
    _copy_summary_fields(inputs, summary)
    return inputs


def read_checkpoint(workspace: Path) -> dict[str, Any]:
    latest = _latest_execution_dir(workspace)
    if latest is None:
        return {}
    path = latest / EXECUTION_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("checkpoint file corrupted or unreadable: %s — %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def normalize_resume_task(task: str) -> str:
    prefixes = (
        "Continue this MokioClaw task from the checkpoint:",
        "Continue the interrupted MokioClaw task from the checkpoint:",
    )
    normalized = task.strip()
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                changed = True
    return normalized


def read_checkpoint_text(workspace: Path, name: str) -> str:
    latest = _latest_execution_dir(workspace)
    if latest is None:
        return ""
    path = latest / name
    if not path.exists():
        return ""
    try:
        return trim_text(path.read_text(encoding="utf-8", errors="replace"), MAX_RECOVERY_TEXT)
    except OSError:
        return ""


def read_workspace_text(workspace: Path, name: str) -> str:
    path = workspace / name
    if not path.exists():
        return ""
    try:
        return trim_text(path.read_text(encoding="utf-8", errors="replace"), 2400)
    except OSError:
        return ""


def read_workspace_text_any(workspace: Path, *names: str) -> str:
    """按优先级返回第一个非空内容（用于内部工作文件迁移：.mokioclaw/ 新路径 → 根目录旧路径）"""
    for name in names:
        text = read_workspace_text(workspace, name)
        if text:
            return text
    return ""


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in state.items():
        if key == "runtime":
            continue
        if key == "messages" and isinstance(value, list):
            serialized[key] = [serialize_message(message) for message in value]
            continue
        serialized[key] = json_safe(value)
    return serialized


def deserialize_state(data: dict[str, Any], runtime: Any) -> dict[str, Any]:
    state = dict(data)
    messages = state.get("messages")
    if isinstance(messages, list):
        state["messages"] = deserialize_messages(messages)
    else:
        state["messages"] = []
    state["runtime"] = runtime
    return state


def serialize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, BaseMessage):
        return message_to_dict(message)
    return json_safe(message)


def deserialize_messages(messages: list[Any]) -> list[BaseMessage]:
    typed_messages = [message for message in messages if isinstance(message, dict) and "type" in message and "data" in message]
    if len(typed_messages) != len(messages):
        return []
    return list(messages_from_dict(typed_messages))


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    todos = state.get("todos", [])
    sources = state.get("sources", [])
    return {
        "plan_summary": trim_text(state.get("plan_summary", ""), 1200),
        "todos": json_safe(todos),
        "todo_count": len(todos) if isinstance(todos, list) else 0,
        "acceptance_criteria": json_safe(state.get("acceptance_criteria", [])),
        "verification_commands": json_safe(state.get("verification_commands", [])),
        "passed": state.get("passed"),
        "attempts": state.get("attempts", 0),
        "sources": json_safe(sources[:10] if isinstance(sources, list) else []),
        "source_count": len(sources) if isinstance(sources, list) else 0,
        "research_notes": trim_text(state.get("research_notes", ""), 1600),
        "code_agent_summary": trim_text(state.get("code_agent_summary", "") or state.get("last_actor_summary", ""), 1600),
        "verifier_summary": trim_text(state.get("verifier_summary", ""), 1600),
        "last_error": trim_text(state.get("last_error", ""), 1600),
        "context_summary": trim_text(state.get("context_summary", ""), 1800),
        "history_summary": trim_text(state.get("history_summary", ""), 1800),
        "final_answer": trim_text(state.get("final_answer", ""), 1800),
    }


def build_recovery_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    manifest = payload.get("workspace_manifest") if isinstance(payload.get("workspace_manifest"), list) else []
    todos = summary.get("todos") if isinstance(summary.get("todos"), list) else []
    sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
    commands = summary.get("verification_commands") if isinstance(summary.get("verification_commands"), list) else []
    criteria = summary.get("acceptance_criteria") if isinstance(summary.get("acceptance_criteria"), list) else []

    lines = [
        "# MokioClaw Recovery",
        "",
        f"- status: {payload.get('status', '')}",
        f"- mode: {payload.get('mode', '')}",
        f"- updated_at: {payload.get('updated_at', '')}",
        f"- latest_node: {payload.get('latest_node', '')}",
        f"- next_node: {payload.get('next_node', '')}",
        f"- attempts: {payload.get('attempts', 0)} / {payload.get('max_attempts', 0)}",
        f"- workspace: {payload.get('workspace', '')}",
        f"- resume: `{payload.get('resume_command', '')}`",
        "",
        "## Task",
        trim_text(payload.get("task", ""), 1200) or "(unknown)",
        "",
        "## Plan",
        summary.get("plan_summary") or "(none)",
        "",
        "## Todos",
    ]
    lines.extend(_markdown_items([f"[{todo.get('status', '')}] {todo.get('content', '')}" for todo in todos]))
    lines.extend(["", "## Acceptance Criteria"])
    lines.extend(_markdown_items(criteria))
    lines.extend(["", "## Verification Commands"])
    lines.extend(_markdown_items(commands))
    lines.extend(["", "## Sources"])
    lines.extend(_markdown_items([f"{source.get('title', '')}: {source.get('url', '')}" for source in sources]))
    lines.extend(
        [
            "",
            "## Recent Summaries",
            f"- research: {summary.get('research_notes') or '(none)'}",
            f"- codeAgent: {summary.get('code_agent_summary') or '(none)'}",
            f"- verifier: {summary.get('verifier_summary') or '(none)'}",
            f"- last_error: {summary.get('last_error') or '(none)'}",
            "",
            "## Recent Files",
        ]
    )
    lines.extend(_markdown_items([f"{item.get('path', '')} ({item.get('size', 0)} bytes)" for item in manifest[:40]]))
    return "\n".join(lines).rstrip() + "\n"


def workspace_manifest(workspace: Path, *, limit: int = MAX_MANIFEST_ITEMS) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not workspace.exists():
        return items
    for path in sorted(workspace.rglob("*")):
        if len(items) >= limit:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if should_skip_workspace_path(rel):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "path": rel.as_posix(),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return items


def snapshot_workspace_git(workspace: Path, root: Path) -> tuple[str | None, str | None]:
    if shutil.which("git") is None:
        return None, "git executable not found"

    workspace = workspace.resolve()
    root = root.resolve()
    shared_git_dir = root / GIT_SHARED_REPO
    shared_git_dir.mkdir(parents=True, exist_ok=True)
    try:
        _init_shared_git(shared_git_dir)
        _ensure_git_excludes(shared_git_dir)
        _git(workspace, shared_git_dir, ["add", "-A", "--", "."])
        status = _git(workspace, shared_git_dir, ["status", "--porcelain"]).stdout.strip()
        head = git_head(workspace, shared_git_dir)
        if not status and head:
            return head, None
        args = ["commit", "-q", "-m", f"{GIT_COMMIT_PREFIX} {utc_now()}"]
        if not status:
            args.append("--allow-empty")
        _git(workspace, shared_git_dir, args)
        return git_head(workspace, shared_git_dir), None
    except Exception as exc:
        return git_head(workspace, shared_git_dir), f"{type(exc).__name__}: {exc}"


def _init_shared_git(git_dir: Path) -> None:
    """初始化/验证共享 git 仓库（幂等）"""
    if not (git_dir / "HEAD").exists():
        _git_direct(git_dir, ["init", "-q", "--bare"])
    _git_direct(git_dir, ["config", "user.name", "MokioClaw Checkpoint"])
    _git_direct(git_dir, ["config", "user.email", "mokioclaw-checkpoint@example.local"])


def git_head(workspace: Path, git_dir: Path) -> str | None:
    try:
        result = _git(workspace, git_dir, ["rev-parse", "--short", "HEAD"])
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def resume_command(workspace: Path) -> str:
    return f"uv run mokioclaw --resume {shlex.quote(str(workspace))}"


def trim_text(value: Any, limit: int) -> str:
    """截断任意值（先 JSON 序列化再截断）"""
    return truncate_json(value, limit)


def should_skip_workspace_path(rel: Path) -> bool:
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == ".mokioclaw" and parts[1] == "executions":
        return True
    skip_names = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
    return any(part in skip_names for part in parts)


def _copy_summary_fields(inputs: dict[str, Any], summary: dict[str, Any]) -> None:
    for key in (
        "plan_summary",
        "todos",
        "acceptance_criteria",
        "verification_commands",
        "sources",
        "research_notes",
        "code_agent_summary",
        "verifier_summary",
        "last_error",
    ):
        if key in summary and summary[key]:
            inputs[key] = summary[key]


def _markdown_items(items: list[Any]) -> list[str]:
    return [f"- {item}" for item in items if item] or ["- (none)"]


_write_json = write_json


class CheckpointMeta:
    """检查点元数据"""

    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.dir = path.parent
        self.checkpoint_id = self.dir.name
        self.mode = payload.get("mode", "light")
        self.status = payload.get("status", "")
        self.updated_at = payload.get("updated_at", "")
        self.latest_node = payload.get("latest_node", "")
        self.next_node = payload.get("next_node", "")
        self.task = payload.get("task", "")
        self.attempts = payload.get("attempts", 0)
        self.max_attempts = payload.get("max_attempts", 0)
        self.workspace = Path(payload.get("workspace", "")) if payload.get("workspace") else None
        self.git_commit = ((payload.get("git") or {}).get("commit"))
        self.has_state = bool(payload.get("state_file"))

    @property
    def execution_file(self) -> Path:
        return self.dir / EXECUTION_FILE

    @property
    def state_file(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def recovery_file(self) -> Path:
        return self.dir / RECOVERY_FILE

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "mode": self.mode,
            "status": self.status,
            "updated_at": self.updated_at,
            "latest_node": self.latest_node,
            "next_node": self.next_node,
            "task": self.task,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "workspace": str(self.workspace) if self.workspace else "",
            "git_commit": self.git_commit,
            "has_state": self.has_state,
        }


def list_executions(workspace: Path) -> list[CheckpointMeta]:
    """列出 workspace 中所有可用的执行记录

    Args:
        workspace: 工作区路径

    Returns:
        执行元数据列表，按更新时间降序
    """
    root = workspace / EXECUTIONS_ROOT
    if not root.exists():
        return []

    executions: list[CheckpointMeta] = []
    for exec_dir in sorted(root.iterdir()):
        if not exec_dir.is_dir() or not exec_dir.name.startswith("exec-"):
            continue
        path = exec_dir / EXECUTION_FILE
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                executions.append(CheckpointMeta(path, payload))
        except (OSError, json.JSONDecodeError):
            continue

    executions.sort(key=lambda e: e.updated_at, reverse=True)
    return executions


def rollback_to_execution(
    workspace: Path,
    execution_id: str,
    *,
    restore_workspace_files: bool = True,
) -> dict[str, Any]:
    """回滚到指定的执行记录

    Args:
        workspace: 工作区路径
        execution_id: 执行 ID（execution.json 的目录名，如 exec-20260818-103000-ab12cd）
        restore_workspace_files: 是否从 git snapshot 恢复工作区文件

    Returns:
        执行 payload（strict 模式下包含 _restored_state）

    Raises:
        FileNotFoundError: 执行记录不存在
        ValueError: 执行数据无效
    """
    root = workspace / EXECUTIONS_ROOT
    execution_file = root / execution_id / EXECUTION_FILE
    if not execution_file.exists():
        raise FileNotFoundError(f"execution not found: {execution_file}")

    payload = json.loads(execution_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid execution payload: {execution_file}")

    # 恢复工作区文件（从共享 git 快照，使用该 execution 的 commit）
    if restore_workspace_files:
        shared_git_dir = snapshots_dir(workspace)
        git_info = payload.get("git") or {}
        commit = git_info.get("commit")
        if commit and shared_git_dir.exists():
            try:
                _git(workspace, shared_git_dir, ["checkout", "-f", commit, "--", "."])
            except Exception as exc:
                logger.warning("workspace restore from checkpoint failed: %s", exc)

    # 恢复 state.json（strict 模式）
    state_path = root / execution_id / STATE_FILE
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            payload["_restored_state"] = state_data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("rollback state restore failed: %s", exc)

    return payload


def _git(workspace: Path, git_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={workspace}", *args],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    )


def _git_direct(git_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行 git 命令（不需要 work-tree，用于 bare 仓库初始化）"""
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        text=True,
        capture_output=True,
    )


# 给共享快照 Git 仓库配置忽略清单
def _ensure_git_excludes(git_dir: Path) -> None:
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    patterns = [
        ".mokioclaw/executions/",
        ".mokioclaw/snapshots/",
        ".venv/",
        "venv/",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
    ]
    with exclude_path.open("a", encoding="utf-8") as handle:
        for pattern in patterns:
            if pattern not in existing:
                handle.write(pattern + "\n")
