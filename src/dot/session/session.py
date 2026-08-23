"""
Session 数据结构 — State IS Session

设计原则：
  - Session 是内存唯一权威源，所有字段直接挂在 Session 对象上
  - messages 是唯一跨 turn 的字段，其余每 turn reset
  - 节点直接读写 session.xxx，返回值仅用于事件流（路由也读 session）
  - 不再需要 RuntimeState 中间层，per-session 运行时状态直接在 Session 上
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from ..compress.state import CompressionState
from ..core.log import get_logger
from ..core.tool_result_budget import ToolResultBudget

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

    # --- 跨 turn 持久字段 ---
    messages: list[BaseMessage] = field(default_factory=list)

    # --- 每 turn 重置字段（执行前清空，节点写入）---
    task: str = ""
    is_running: bool = False
    task_plan: dict = field(default_factory=dict)
    replan_count: int = 0
    attempt_count: int = 0
    validate_result: dict = field(default_factory=dict)
    need_human_intervene: bool = False
    resume_action: str = ""
    plan_invalid: bool = False
    awaiting_intervention: bool = False

    # --- 会话级字段（跨 turn 持久，不随 reset_per_turn 清除）---
    workspace: Path = field(default_factory=Path.cwd)
    _is_running_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    # 取消令牌：CLI 层 Ctrl+C 设置，graph stream worker 在节点边界检查并提前结束
    _cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    replan_max: int = REPLAN_THRESHOLD
    max_attempt: int = MAX_ATTEMPT_DEFAULT
    current_turn_id: int = 0

    # --- 共享设施（由 SharedServices 注入，全会话复用）---
    compiled_graph: Any = None
    persistence: Any = None
    mcp_host: Any = None
    skill_host: Any = None
    hook_runner: Any = None

    # --- per-session 运行时状态（原 RuntimeState，直接平铺到 Session）---
    agent_mode: str = "auto"
    # CLI 运行模式：agent（完整）/ chat（纯对话）/ code（代码专注）
    run_mode: str = "agent"
    # MCP 渐进披露：已按需加载的工具名 → StructuredTool
    loaded_mcp_tools: dict[str, Any] = field(default_factory=dict)
    # Skill 渐进披露：已加载的 skill 名集合 + 累计注入正文
    loaded_skills: set[str] = field(default_factory=set)
    active_skill_content: str = ""
    # 文件快照：追踪已读取文件状态，防止覆盖并发修改
    read_files: dict = field(default_factory=dict)  # dict[Path, FileSnapshot]
    # 工具输出预算，大输出自动落盘
    result_budget: Any = field(default_factory=ToolResultBudget)
    # BashTool 配置
    cwd: Path | None = None
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # 消息序号（并行工具调用时加锁）
    _message_seq: int = 0
    _message_seq_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # --- 上下文压缩状态（跨 turn 持久，不随 reset_per_turn 清除）---
    compression_state: CompressionState = field(default_factory=CompressionState)

    def reset_per_turn(self) -> None:
        """每轮执行前重置 per-turn 字段（messages / 运行时状态保留）"""
        logger.debug("[Session] reset_per_turn: session=%s", self.session_id)
        self.task = ""
        self.is_running = False
        self.task_plan = {}
        self.replan_count = 0
        self.attempt_count = 0
        self.validate_result = {}
        self.need_human_intervene = False
        self.resume_action = ""
        self.plan_invalid = False
        self.awaiting_intervention = False

    def next_message_id(self) -> str:
        """生成递增消息序号（线程安全）"""
        with self._message_seq_lock:
            self._message_seq += 1
            return f"msg-{self._message_seq:05d}"

    # ----------------------------------------------------------
    # 并发守卫（每会话一次一个 turn）
    # ----------------------------------------------------------

    def acquire_run(self) -> bool:
        """原子地检查并占用 turn 执行权。"""
        with self._is_running_lock:
            if self.is_running:
                return False
            self.is_running = True
            return True

    def release_run(self) -> None:
        """释放 turn 执行权（幂等）。"""
        with self._is_running_lock:
            self.is_running = False
