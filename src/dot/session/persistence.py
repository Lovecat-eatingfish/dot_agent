"""
会话持久化 + rewind 回滚（dot 独立实现）

存储策略（对齐 doc/fix.md）：
  - 所有会话共享 .dot/sessions/ 根目录
  - 每个 session 有独立子目录：.dot/sessions/{session_id}/
  - session.json 全量覆盖写入（每 turn 完整替换）
  - turns/turn_{nnnn}.json 快照（用于 rewind）
  - 用户代码回滚用 agent 专用 git（GIT_DIR=<workspace>/.dot/git，
    work-tree=用户项目目录），每轮 commit，hash 存入 turn 快照
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.git_utils import agent_git_commit, agent_git_reset_hard
from ..core.log import get_logger
from ..trace import get_tracer
from ..core.utils import utc_now

logger = get_logger(__name__)

SESSIONS_DIR = ".dot/sessions"
SESSION_META_FILE = "session.json"
TURNS_DIR = "turns"
TURN_FILE_PREFIX = "turn_"
TURN_FILE_SUFFIX = ".json"


# ============================================================
# Helpers
# ============================================================

def _safe_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    return str(value)


# ============================================================
# Message serialization
# ============================================================

def serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """BaseMessage 列表 → JSON 安全 dict 列表"""
    result = []
    for msg in messages:
        if hasattr(msg, "to_dict"):
            entry = msg.to_dict()
            # to_dict 的结构是 {"type": "human"/"ai"/..., "data": {...}}，
            # 统一拍平为反序列化友好的格式
            data = entry.get("data", {}) if isinstance(entry, dict) else {}
            flat = {
                "type": type(msg).__name__,
                "content": _safe_json(data.get("content", getattr(msg, "content", ""))),
            }
            if getattr(msg, "tool_call_id", None):
                flat["tool_call_id"] = getattr(msg, "tool_call_id", "")
            name = data.get("name") or getattr(msg, "name", None)
            if name:
                flat["name"] = name
            tool_calls = data.get("tool_calls") or getattr(msg, "tool_calls", None)
            if tool_calls:
                flat["tool_calls"] = _safe_json(tool_calls)
            if getattr(msg, "id", None):
                flat["id"] = getattr(msg, "id")
            result.append(flat)
        else:
            entry: dict[str, Any] = {
                "type": type(msg).__name__,
                "content": _safe_json(getattr(msg, "content", "")),
            }
            if hasattr(msg, "tool_call_id"):
                entry["tool_call_id"] = getattr(msg, "tool_call_id", "")
            if getattr(msg, "name", None):
                entry["name"] = getattr(msg, "name", "")
            result.append(entry)
    return result


def deserialize_messages(serialized: list[dict[str, Any]]) -> list[Any]:
    """JSON dict 列表 → BaseMessage 列表"""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    type_map = {
        "HumanMessage": HumanMessage,
        "AIMessage": AIMessage,
        "SystemMessage": SystemMessage,
        "ToolMessage": ToolMessage,
    }
    result = []
    for item in serialized:
        msg_type = item.get("type", "")
        cls = type_map.get(msg_type)
        if cls is None:
            continue
        kwargs: dict[str, Any] = {"content": item.get("content", "")}
        if msg_type == "ToolMessage":
            kwargs["tool_call_id"] = item.get("tool_call_id", "")
            name_val = item.get("name")
            if name_val:
                kwargs["name"] = name_val
        elif msg_type == "AIMessage" and item.get("tool_calls"):
            kwargs["tool_calls"] = item["tool_calls"]
        if item.get("id"):
            kwargs["id"] = item["id"]
        try:
            result.append(cls(**kwargs))
        except Exception:
            # 兜底：损坏条目降级为 HumanMessage
            result.append(HumanMessage(content=str(kwargs.get("content", ""))))
    return result


# ============================================================
# SessionPersistence
# ============================================================

@dataclass
class SessionPersistence:
    """会话持久化器，封装所有磁盘 / git 操作

    存储位置：.dot/sessions/{session_id}/
    """
    sessions_root: Path

    def __post_init__(self) -> None:
        if isinstance(self.sessions_root, str):
            self.sessions_root = Path(self.sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_root / session_id

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / SESSION_META_FILE

    def turns_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / TURNS_DIR

    # ----------------------------------------------------------
    # session.json 读写（全量覆盖，原子写）
    # ----------------------------------------------------------

    def load_session_meta(self, session_id: str) -> dict[str, Any]:
        path = self.session_meta_path(session_id)
        if not path.exists():
            logger.debug("[persistence] session.json not found: %s", path)
            return self._empty_session_meta(session_id)
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            logger.debug("[persistence] loaded session.json: %s (msgs=%d)", session_id, len(meta.get("messages", [])))
            return meta
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[persistence] load session.json failed for %s: %s", session_id, exc)
            return self._empty_session_meta(session_id)

    def save_session_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        path = self.session_meta_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.debug("[persistence] saved session.json: %s (msgs=%d)", session_id, len(meta.get("messages", [])))

    def _empty_session_meta(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "current_turn_id": 0,
            "task": "",
            "workspace": "",
            "messages": [],
            "replan_count": 0,
            "attempt_count": 0,
            "awaiting_intervention": False,
        }

    # ----------------------------------------------------------
    # Turn 快照读写
    # ----------------------------------------------------------

    def write_turn_snapshot(
        self,
        session_id: str,
        turn_id: int,
        git_commit_hash: str,
        final_state: dict[str, Any],
        full_messages: list[Any],
    ) -> None:
        turns = self.turns_dir(session_id)
        turns.mkdir(parents=True, exist_ok=True)
        path = turns / f"{TURN_FILE_PREFIX}{turn_id:04d}{TURN_FILE_SUFFIX}"

        snapshot = {
            "turn_id": turn_id,
            "git_commit_hash": git_commit_hash,
            "timestamp": utc_now(),
            "graph_state": _serialize_state(final_state),
            "full_messages": serialize_messages(full_messages),
        }
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def read_turn_snapshot(
        self, session_id: str, turn_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self._turn_path(session_id, turn_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("graph_state", {}), raw

    def list_available_turns(self, session_id: str) -> list[int]:
        turns = self.turns_dir(session_id)
        if not turns.exists():
            return []
        ids: list[int] = []
        for path in sorted(turns.glob(f"{TURN_FILE_PREFIX}*{TURN_FILE_SUFFIX}")):
            stem = path.stem  # turn_0001
            try:
                turn_id = int(stem.split("_")[-1])
                ids.append(turn_id)
            except (ValueError, IndexError):
                continue
        return ids

    # ----------------------------------------------------------
    # Rewind 回滚
    # ----------------------------------------------------------

    def rewind_to_turn(
        self,
        session_id: str,
        target_turn_id: int,
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """回滚到指定 turn

        步骤：
        1. 校验 turn 存在，读取快照
        2. agent 专用 git reset --hard 恢复用户代码（hash 来自快照）
        3. 恢复 session.json（消息 + 元信息）
        4. 返回 graph_state 供内存恢复（SessionManager 负责）
        """
        graph_state, snapshot = self.read_turn_snapshot(session_id, target_turn_id)
        git_hash = snapshot.get("git_commit_hash", "")

        # 用户代码回滚（agent 专用 repo，失败告警不中断）
        if git_hash and workspace is not None:
            try:
                agent_git_reset_hard(workspace, git_hash)
            except Exception as exc:
                logger.warning("rewind_to_turn: git reset failed (%s)", exc)

        # 恢复 session.json
        full_messages = snapshot.get("full_messages", [])
        meta = self._empty_session_meta(session_id)
        meta["messages"] = full_messages
        meta["current_turn_id"] = target_turn_id
        meta["replan_count"] = graph_state.get("replan_count", 0)
        meta["attempt_count"] = graph_state.get("attempt_count", 0)
        meta["awaiting_intervention"] = bool(graph_state.get("awaiting_intervention", False))
        if workspace is not None:
            meta["workspace"] = str(workspace)
        meta["updated_at"] = utc_now()
        self.save_session_meta(session_id, meta)

        return graph_state

    # ----------------------------------------------------------
    # Internal
    # ----------------------------------------------------------

    def _turn_path(self, session_id: str, turn_id: int) -> Path:
        return self.turns_dir(session_id) / f"{TURN_FILE_PREFIX}{turn_id:04d}{TURN_FILE_SUFFIX}"


# ============================================================
# Turn 持久化（finally 节点调用）
# ============================================================

def persist_turn(
    persistence: SessionPersistence,
    session_id: str,
    turn_id: int,
    session: Any,  # Session — 避免循环 import
) -> None:
    """graph 运行结束后持久化（finally 节点内调用）

    步骤：
    1. 全量写入 session.json（messages + 元信息）
    2. agent 专用 git commit 用户项目目录（代码回滚锚点）
    3. 写入 turn 快照（含 git hash，供 rewind）
    """
    full_messages = list(session.messages)
    logger.info("[persist_turn] session=%s, turn=%d, messages=%d", session_id, turn_id, len(full_messages))
    span = get_tracer().start_span(
        "session", "persist_turn",
        tags={"turn_id": turn_id, "messages": len(full_messages)},
    )

    # 全量 session.json
    meta = {
        "session_id": session_id,
        "created_at": _read_created_at(persistence, session_id),
        "updated_at": utc_now(),
        "current_turn_id": turn_id,
        "task": getattr(session, "task", ""),
        "workspace": str(getattr(session, "workspace", "")),
        "messages": serialize_messages(full_messages),
        "replan_count": session.replan_count,
        "attempt_count": session.attempt_count,
        "awaiting_intervention": bool(getattr(session, "awaiting_intervention", False)),
    }
    persistence.save_session_meta(session_id, meta)

    # agent 专用 git commit（用户项目目录快照，rewind 恢复代码用）
    git_hash = ""
    workspace = getattr(session, "workspace", None)
    if workspace is not None:
        try:
            git_hash = agent_git_commit(workspace, f"[dot] {session_id} turn {turn_id}")
            if git_hash:
                logger.info("[persist_turn] git commit: %s", git_hash[:8])
        except Exception as exc:
            logger.debug("git commit skipped: %s", exc)

    # turn 快照
    persistence.write_turn_snapshot(
        session_id=session_id,
        turn_id=turn_id,
        git_commit_hash=git_hash,
        final_state=_session_to_state(session),
        full_messages=full_messages,
    )
    span.set_output_summary(f"git={git_hash[:8] if git_hash else 'none'}")
    span.finish()


def _read_created_at(persistence: SessionPersistence, session_id: str) -> str:
    meta = persistence.load_session_meta(session_id)
    return meta.get("created_at", utc_now())


def _session_to_state(session: Any) -> dict[str, Any]:
    """从 Session 对象提取 graph state dict（用于快照）"""
    return {
        "task": session.task,
        "task_plan": session.task_plan,
        "replan_count": session.replan_count,
        "attempt_count": session.attempt_count,
        "validate_result": session.validate_result,
        "need_human_intervene": session.need_human_intervene,
        "resume_action": session.resume_action,
        "plan_invalid": session.plan_invalid,
        "awaiting_intervention": bool(getattr(session, "awaiting_intervention", False)),
    }


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in state.items():
        result[key] = _safe_json(value)
    return result
