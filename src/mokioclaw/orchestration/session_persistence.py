"""
会话持久化 + rewind 回滚模块

职责：
- session.json / turn_xx.json 读写
- git commit 占位封装
- diff_messages 工具
- rewind_to_turn 回滚逻辑

约束：graph 节点内部禁止磁盘 IO / git 调用，所有持久化操作
全部放在 graph 外部的 stream_session_events 结束后的外部层处理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mokioclaw.reliability.git_utils import git_commit, git_init, git_reset_hard


# ============================================================
# Constants
# ============================================================

AGENT_SESSIONS_DIR = ".agent_sessions"
SESSION_META_FILE = "session.json"
TURNS_DIR = "turns"
TURN_FILE_PREFIX = "turn_"
TURN_FILE_SUFFIX = ".json"


# ============================================================
# Message diff utility
# ============================================================

def diff_messages(
    old_full: list[Any],
    state_messages: list[Any],
) -> list[Any]:
    """对比启动前完整消息与 graph 结束后 state.messages，提取本轮新消息

    逻辑：取 state_messages 尾部不在 old_full 中的消息。
    使用 id 字段匹配；无 id 时回退到内容比较。

    Args:
        old_full: graph 启动前的完整消息列表（来自 session.json）
        state_messages: graph 结束后 state.messages（可能是压缩后的运行时拷贝）

    Returns:
        本轮新生成的消息集合
    """
    if not old_full:
        return list(state_messages)

    # 构建旧消息索引：按 id → 位置
    old_by_id: dict[str, int] = {}
    for i, msg in enumerate(old_full):
        msg_id = _get_message_id(msg)
        old_by_id[msg_id] = i

    # 从尾部扫描，找到第一个在 old_full 中存在的消息位置
    new_messages: list[Any] = []
    for msg in reversed(state_messages):
        msg_id = _get_message_id(msg)
        if msg_id in old_by_id:
            # 这条消息在旧列表中已存在，停止
            break
        new_messages.insert(0, msg)

    return new_messages


def _get_message_id(msg: Any) -> str:
    """提取消息 id，无 id 时回退到内容哈希"""
    msg_id = getattr(msg, "id", None)
    if msg_id:
        return str(msg_id)
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return f"content:{hash(content)}"
    return f"type:{type(msg).__name__}"


# ============================================================
# Session Persistence
# ============================================================

@dataclass
class SessionPersistence:
    """会话持久化器，封装所有磁盘 / git 操作

    存储位置始终在用户编码空间的 .agent_sessions/ 子目录下：
      sessions_root = workspace / ".agent_sessions"
    这样 agent 内部存储（session.json、turns/）和用户代码在同一棵树里。
    """

    sessions_root: Path
    workspace: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.workspace is not None:
            self.sessions_root = self.workspace / ".agent_sessions"

    def update_workspace(self, workspace: Path) -> None:
        """用户编码空间变更时，同步更新存储根目录"""
        self.workspace = workspace
        self.sessions_root = workspace / ".agent_sessions"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_root / session_id

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / SESSION_META_FILE

    def turns_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / TURNS_DIR

    # ----------------------------------------------------------
    # session.json 读写
    # ----------------------------------------------------------

    def load_session_meta(self, session_id: str) -> dict[str, Any]:
        """读取 session.json，不存在则返回空骨架"""
        path = self.session_meta_path(session_id)
        if not path.exists():
            return self._empty_session_meta(session_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._empty_session_meta(session_id)

    def save_session_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        """写回 session.json（原子写）"""
        path = self.session_meta_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def _empty_session_meta(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "current_turn_id": 0,
            "messages": [],
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
        """写入 turn_{turn_id}.json 快照"""
        turns = self.turns_dir(session_id)
        turns.mkdir(parents=True, exist_ok=True)
        path = turns / f"{TURN_FILE_PREFIX}{turn_id:04d}{TURN_FILE_SUFFIX}"

        snapshot = {
            "turn_id": turn_id,
            "git_commit_hash": git_commit_hash,
            "timestamp": _now_iso(),
            "graph_state": _serialize_state(final_state),
            "full_messages": _serialize_messages(full_messages),
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def read_turn_snapshot(
        self, session_id: str, turn_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取 turn 快照，返回 (graph_state, snapshot_raw_dict)"""
        path = self._turn_path(session_id, turn_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("graph_state", {}), raw

    def list_available_turns(self, session_id: str) -> list[int]:
        """扫描 turns 目录，返回所有合法 turn_id 列表（升序）"""
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

    def rewind_to_turn(self, session_id: str, target_turn_id: int) -> dict[str, Any]:
        """回滚到指定 turn

        步骤：
        1. 校验 turn 存在，读取快照
        2. git reset --hard
        3. 恢复 session.json["messages"] ← 快照 full_messages
        4. 更新 session.json current_turn_id + updated_at
        5. 返回 graph_state（供内存 Session 恢复）
        """
        # 1. 校验并读取快照
        graph_state, snapshot = self.read_turn_snapshot(session_id, target_turn_id)
        git_hash = snapshot.get("git_commit_hash", "")
        workspace = self.session_dir(session_id)

        # 2. git reset
        if git_hash:
            git_reset_hard(workspace, git_hash)

        # 3. 恢复 session.json messages
        full_messages = snapshot.get("full_messages", [])
        meta = self.load_session_meta(session_id)
        meta["messages"] = full_messages
        meta["current_turn_id"] = target_turn_id
        meta["updated_at"] = _now_iso()

        # 4. 写回 session.json: todo 可能需要最后做持久化就好了，不需要在这里做
        self.save_session_meta(session_id, meta)

        # 5. 返回 graph_state 供内存恢复
        return graph_state

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _turn_path(self, session_id: str, turn_id: int) -> Path:
        turns = self.turns_dir(session_id)
        return turns / f"{TURN_FILE_PREFIX}{turn_id:04d}{TURN_FILE_SUFFIX}"


# ============================================================
# Post-run persistence helper
# ============================================================

def persist_turn(
    persistence: SessionPersistence,
    session_id: str,
    turn_id: int,
    workspace: Path,
    old_full_messages: list[Any],
    final_state: dict[str, Any],
) -> None:
    """graph 运行结束后，在外部层执行持久化

    步骤：
    1. diff 得到本轮新消息
    2. 追加到 session.json["messages"]
    3. git commit
    4. 写入 turn 快照
    5. 更新 session.json 元信息

    Args:
        persistence: 持久化器
        session_id: 会话 ID
        turn_id: 当前 turn 编号
        workspace: git 仓库路径
        old_full_messages: graph 启动前的完整消息列表
        final_state: graph 最终 state（含压缩后的 messages）
    """
    # 1. diff 新消息
    state_messages = final_state.get("messages", [])
    new_messages = diff_messages(old_full_messages, state_messages)

    # 2. 追加到 session.json
    meta = persistence.load_session_meta(session_id)
    existing = meta.get("messages", [])
    # 避免重复追加（幂等保护）
    if new_messages and not _already_ends_with(existing, new_messages):
        existing.extend(_deserialize_messages(new_messages))
    meta["messages"] = existing
    meta["current_turn_id"] = turn_id
    meta["updated_at"] = _now_iso()

    # 3. git commit
    git_hash = ""
    try:
        git_hash = git_commit(workspace, f"turn {turn_id}")
    except Exception:
        pass

    # 4. 写入 turn 快照
    persistence.write_turn_snapshot(
        session_id=session_id,
        turn_id=turn_id,
        git_commit_hash=git_hash,
        final_state=final_state,
        full_messages=existing,
    )

    # 5. 写回 session.json
    persistence.save_session_meta(session_id, meta)


# ============================================================
# Serialization helpers
# ============================================================

def _serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """将 BaseMessage 列表序列化为可 JSON 存储的字典列表"""
    result = []
    for msg in messages:
        if hasattr(msg, "to_dict"):
            result.append(msg.to_dict())
        else:
            result.append({
                "type": type(msg).__name__,
                "content": _safe_json(msg.content),
            })
    return result


def _deserialize_messages(serialized: list[dict[str, Any]]) -> list[Any]:
    """反序列化消息字典列表为 BaseMessage 对象"""
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
        if cls:
            content = item.get("content", "")
            kwargs: dict[str, Any] = {"content": content}
            if msg_type == "ToolMessage":
                kwargs["tool_call_id"] = item.get("tool_call_id", "")
            result.append(cls(**kwargs))
    return result


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """序列化 GraphState，只序列化可 JSON 的字段"""
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "messages":
            result[key] = _serialize_messages(value)
        elif key == "task_plan":
            result[key] = value
        elif key == "tool_artifacts":
            result[key] = value
        elif key == "validate_result":
            result[key] = value
        else:
            try:
                json.dumps(value)
                result[key] = value
            except (TypeError, ValueError):
                result[key] = str(value)
    return result


def _already_ends_with(existing: list[Any], new_messages: list[Any]) -> bool:
    """检查 existing 列表是否已以 new_messages 结尾（幂等保护）"""
    if len(new_messages) > len(existing):
        return False
    for i, new_msg in enumerate(new_messages):
        old_msg = existing[-len(new_messages) + i]
        if _get_message_id(old_msg) != _get_message_id(new_msg):
            return False
    return True


def _safe_json(value: Any) -> Any:
    """安全地将值转为 JSON 兼容格式"""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [_safe_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _safe_json(v) for k, v in value.items()}
    return str(value)


def _now_iso() -> str:
    """返回当前 UTC 时间 ISO 格式字符串"""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
