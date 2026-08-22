"""
Session 数据结构 — State IS Session

设计原则：
  - Session 是内存唯一权威源，所有字段直接挂在 Session 对象上
  - messages 是唯一跨 turn 的字段，其余每 turn reset
  - 节点直接读写 session.xxx，返回值仅用于事件流（路由也读 session）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from ..core.log import get_logger

logger = get_logger(__name__)


# ============================================================
# Constants
# ============================================================

REPLAN_THRESHOLD = 3
MAX_ATTEMPT_DEFAULT = 3


@dataclass
class Session:
    """单个会话的内存数据（State = Session）

    图状态采用单 channel 包装：DotAgentState = {"session": Session}，
    节点通过 state["session"] 拿到同一个 Session 对象直接读写。
    """

    session_id: str
    compiled_graph: Any = None  # CompiledGraph — 图只编译一次
    # --- 跨 turn 持久字段 ---
    messages: list[BaseMessage] = field(default_factory=list)

    # --- 每 turn 重置字段（执行前清空，节点写入）---
    task: str = ""
    task_plan: dict = field(default_factory=dict)
    replan_count: int = 0
    attempt_count: int = 0
    validate_result: dict = field(default_factory=dict)
    need_human_intervene: bool = False
    resume_action: str = ""
    # coding_agent 判定 plan 无效时置位；plan_node 生成新 plan 后清除
    plan_invalid: bool = False
    # 自定义人工介入：human_intervene 节点置位并随本轮 finally 持久化，
    # 控制台据此提示 continue/stop；reset_per_turn 与 resume 时清除
    awaiting_intervention: bool = False

    # --- 会话级不变字段 ---
    workspace: Path = field(default_factory=Path.cwd)
    is_running: bool = False
    replan_max: int = REPLAN_THRESHOLD
    max_attempt: int = MAX_ATTEMPT_DEFAULT
    current_turn_id: int = 0

    # --- 注入的管理器（会话级，持久化时排除，恢复时重新挂载）---
    persistence: Any = None  # SessionPersistence
    mcp_host: Any = None     # MCPHost（渐进披露，有状态）
    skill_host: Any = None   # SkillHost（渐进披露，有状态）
    authorizer: Any = None   # 审批引擎（预留）
    hook_runner: Any = None  # HookRunner
    runtime: Any = None      # RuntimeState（工具执行运行时）

    def reset_per_turn(self) -> None:
        """每轮执行前重置 per-turn 字段（messages 保留）"""
        logger.debug("[Session] reset_per_turn: session=%s", self.session_id)
        self.task = ""
        self.task_plan = {}
        self.replan_count = 0
        self.attempt_count = 0
        self.validate_result = {}
        self.need_human_intervene = False
        self.resume_action = ""
        self.plan_invalid = False
        self.awaiting_intervention = False
