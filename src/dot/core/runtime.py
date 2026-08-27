"""
RuntimeState — 工具执行时的核心状态容器

职责：
  - 文件快照：追踪已读取文件状态，防止覆盖并发修改（先读后写）
  - Bash 配置：cwd / timeouts / env（由 BashTool 使用）
  - 消息序号：并行工具调用时的线程安全序号生成
  - result_budget：大输出自动落盘

不持有（已移到对应模块）：
  - MCP/Skill/Hook/Permission → AgentContext
  - 渐进披露状态 → AgentContext
  - 审批决策 → PermissionManager
  - per-turn 业务状态 → TurnState
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .path_security import validate_path_access
from .tool_context import FileSnapshot
from .tool_result_budget import ToolResultBudget

VALID_AGENT_MODES = {"auto", "plan", "approve", "edit"}


def normalize_agent_mode(mode: str | None) -> str:
    normalized = (mode or "auto").strip().lower()
    return normalized if normalized in VALID_AGENT_MODES else "auto"


@dataclass
class RuntimeState:
    """工具执行时的核心状态容器（轻量级，文件快照等从 Session 获取）"""

    # 工作区根目录
    workspace: Path

    # Session 引用（文件快照、消息序号、Bash 配置等从 Session 获取）
    session: Any = None

    # Agent 运行模式（权限系统需要）
    agent_mode: str = "auto"

    # 工具权限规则（PermissionManager 已接管）
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    # 工具输出预算
    result_budget: ToolResultBudget = field(default_factory=ToolResultBudget)

    def __post_init__(self) -> None:
        self.agent_mode = normalize_agent_mode(self.agent_mode)

    def next_message_id(self) -> str:
        if self.session is not None:
            return self.session.next_message_id()
        return "msg-00000"

    def record_read(
        self, path: Path, *, complete: bool, content: bytes | str | None = None,
    ) -> None:
        """记录文件读取快照（委托给 Session）"""
        if self.session is not None:
            self.session.record_read(path, complete=complete, content=content)

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        if self.session is not None:
            return self.session.snapshot_for(path)
        return None

    def is_file_modified(self, path: Path) -> bool:
        if self.session is not None:
            return self.session.is_file_modified(path)
        return True

    def assert_workspace_path(self, path: Path, operation: str = "read") -> Path:
        """安全检查：确保路径在工作区内部"""
        return validate_path_access(self, path, operation)
