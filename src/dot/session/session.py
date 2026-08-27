"""
Session — 会话 = 身份 + 状态 + 持久化

职责：
  - 身份：session_id, workspace
  - 消息：messages（跨 turn 持久）
  - per-turn 状态：turn (TurnState)
  - 上下文压缩：compression_state
  - 持久化：persistence（session.json + turn 快照 + git）
  - 工具运行时：文件快照/bash 配置/消息序号（满足 ToolContext 协议）

不持有：
  - 进程级组件（MCP/Skill/Hook/Permission/Tracer/Graph）→ AgentContext
  - 业务状态（plan/validate/intervention）→ TurnState
  - result_budget → AgentContext（独立字段）

Session 本身满足 ToolContext 协议，工具函数可以直接拿 Session 当 state 用。
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage

from ..compress.state import CompressionState
from ..core.log import get_logger
from ..core.tool_context import FileSnapshot
from .turn_state import TurnState

if TYPE_CHECKING:
    from ..session.persistence import SessionPersistence

logger = get_logger(__name__)

REPLAN_THRESHOLD = 3
MAX_ATTEMPT_DEFAULT = 3


# ============================================================
# Session
# ============================================================

@dataclass
class Session:
    """会话数据（State = Session）"""

    session_id: str

    # --- 身份 ---
    workspace: Path = field(default_factory=Path.cwd)

    # --- 跨 turn 持久 ---
    messages: list[BaseMessage] = field(default_factory=list)
    current_turn_id: int = 0
    compression_state: CompressionState = field(default_factory=CompressionState)

    # --- per-turn 状态 ---
    turn: TurnState = field(default_factory=TurnState)

    # --- 持久化 ---
    persistence: "SessionPersistence | None" = None

    # --- 配置 ---
    agent_mode: str = "auto"
    run_mode: str = "agent"
    replan_max: int = REPLAN_THRESHOLD
    max_attempt: int = MAX_ATTEMPT_DEFAULT

    # --- BashTool 配置 ---
    cwd: Path | None = None
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    # --- 文件快照（防止并发修改覆盖）---
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)

    # --- 消息序号（并行工具调用时线程安全）---
    message_seq: int = 0
    _message_seq_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    # --- 并发守卫 ---
    _is_running_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def record_read(
            self, path: Path, *, complete: bool, content: bytes | str | None = None,
    ) -> None:
        """记录文件读取快照"""
        resolved = path.resolve()
        if isinstance(content, bytes):
            raw = content
        else:
            raw = resolved.read_bytes()
        stat = resolved.stat()
        content_hash = hashlib.sha256(raw).hexdigest()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            content_hash=content_hash,
            complete=complete,
        )

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        return self.read_files.get(path.resolve())

    def is_file_modified(self, path: Path) -> bool:
        snap = self.snapshot_for(path)
        if snap is None:
            return True
        resolved = path.resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return True
        if snap.mtime_ns == stat.st_mtime_ns:
            return False
        try:
            current_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return True
        return snap.content_hash != current_hash

    def assert_workspace_path(self, path: Path, operation: str = "read") -> Path:
        """安全检查：确保路径在工作区内部"""
        from ..core.path_security import validate_path_access
        return validate_path_access(self, path, operation)

    def next_message_id(self) -> str:
        with self._message_seq_lock:
            self.message_seq += 1
            return f"msg-{self.message_seq:05d}"

    # ============================================================
    # 恢复辅助
    # ============================================================

    def clear_intervention(self) -> None:
        """清除介入标记，重置计数（resume 前调用）"""
        self.turn.awaiting_intervention = False
        self.turn.resume_action = ""
        self.turn.replan_count = 0
        self.turn.attempt_count = 0
        self.turn.plan_invalid = False
        self.turn.need_human_intervene = False

    def reset_per_turn(self) -> None:
        """每轮执行前重置 per-turn 字段"""
        logger.debug("[Session] reset_per_turn: session=%s", self.session_id)
        self.turn.reset()

    # ============================================================
    # 并发守卫
    # ============================================================

    def acquire_run(self) -> bool:
        """原子地检查并占用 turn 执行权"""
        with self._is_running_lock:
            if self.turn.is_running:
                return False
            self.turn.is_running = True
            return True

    def release_run(self) -> None:
        """释放 turn 执行权（幂等）"""
        with self._is_running_lock:
            self.turn.is_running = False

    # ============================================================
    # 语义化方法（封装 TurnState 访问）
    # ============================================================

    def get_task(self) -> str:
        return self.turn.task

    def set_task(self, task: str) -> None:
        self.turn.task = task

    def get_plan(self) -> dict:
        return self.turn.task_plan

    def set_plan(self, plan: dict) -> None:
        self.turn.task_plan = plan

    def get_replan_count(self) -> int:
        return self.turn.replan_count

    def mark_replan(self) -> None:
        self.turn.replan_count += 1

    def get_attempt_count(self) -> int:
        return self.turn.attempt_count

    def mark_attempt(self) -> None:
        self.turn.attempt_count += 1

    def get_validate_result(self) -> dict:
        return self.turn.validate_result

    def set_validate_result(self, result: dict) -> None:
        self.turn.validate_result = result

    def is_plan_invalid(self) -> bool:
        return self.turn.plan_invalid

    def mark_plan_invalid(self) -> None:
        self.turn.plan_invalid = True

    def clear_plan_invalid(self) -> None:
        self.turn.plan_invalid = False

    def need_human_intervene(self) -> bool:
        return self.turn.need_human_intervene

    def mark_intervene(self) -> None:
        self.turn.need_human_intervene = True

    def clear_intervene_flag(self) -> None:
        self.turn.need_human_intervene = False

    def is_awaiting_intervention(self) -> bool:
        return self.turn.awaiting_intervention

    def get_resume_action(self) -> str:
        return self.turn.resume_action

    def set_awaiting_intervention(self, value: bool) -> None:
        self.turn.awaiting_intervention = value

    def set_resume_action(self, action: str) -> None:
        self.turn.resume_action = action

    def is_turn_running(self) -> bool:
        """Turn 是否正在执行"""
        return self.turn.is_running

    def reset_turn(self) -> None:
        """重置所有 TurnState 字段为默认值（供 manager.py 批量重置用）"""
        self.turn = TurnState()

    def restore_turn(self, data: dict) -> None:
        """从 dict 恢复 TurnState 字段，仅设置 data 中存在的 key（供 manager.py rewind 用）"""
        if not isinstance(data, dict):
            return
        field_map = {
            "task": "task",
            "task_plan": "task_plan",
            "replan_count": "replan_count",
            "attempt_count": "attempt_count",
            "validate_result": "validate_result",
            "need_human_intervene": "need_human_intervene",
            "resume_action": "resume_action",
            "plan_invalid": "plan_invalid",
            "awaiting_intervention": "awaiting_intervention",
            "is_running": "is_running",
        }
        for key, attr in field_map.items():
            if key in data:
                setattr(self.turn, attr, data[key])

    def to_session_meta(self) -> dict:
        """序列化会话元数据（对应 session.json 格式）"""
        from ..core.utils import utc_now
        return {
            "session_id": self.session_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "current_turn_id": self.current_turn_id,
            "workspace": str(self.workspace),
            "run_mode": self.run_mode,
        }

    def to_turn_snapshot(self) -> dict:
        """序列化轮次快照（对应 turn_xxxx.json 格式）"""
        return {
            "task": self.turn.task,
            "task_plan": self.turn.task_plan,
            "replan_count": self.turn.replan_count,
            "attempt_count": self.turn.attempt_count,
            "validate_result": self.turn.validate_result,
            "need_human_intervene": self.turn.need_human_intervene,
            "resume_action": self.turn.resume_action,
            "plan_invalid": self.turn.plan_invalid,
            "awaiting_intervention": self.turn.awaiting_intervention,
        }

    def to_snapshot(self) -> dict:
        """序列化为快照字典（供持久化层使用，合并 session meta + turn snapshot）"""
        from ..core.utils import utc_now
        return {
            "session_id": self.session_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "current_turn_id": self.current_turn_id,
            "task": self.turn.task,
            "workspace": str(self.workspace),
            "replan_count": self.turn.replan_count,
            "attempt_count": self.turn.attempt_count,
            "awaiting_intervention": self.turn.awaiting_intervention,
            "run_mode": self.run_mode,
        }
