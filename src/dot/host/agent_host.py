"""
AgentHost — Agent 统一入口（对齐 doc/fix.md）

初始化全局组件并对外提供统一调用面：
  - SessionManager（内含 SessionPersistence 编排）
  - MCP host（启动时与 server 建连，拉取全部工具列表缓存）
  - Skill host（启动时扫描 .dot/skills 加载全部 skill 元数据）
  - HookRunner（启动时加载 .dot/hooks.json 全部 hook）
  - 审批引擎（预留位置：Session.authorizer + 工具调用前后挂点）

控制台 / 未来的 CLI / TUI 都通过 AgentHost 驱动 agent，
不直接触碰 SessionManager 内部。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

from dot.core.log import get_logger
from dot.constant.session import session_dir
from dot.session.manager import SessionManager

logger = get_logger(__name__)


class AgentHost:
    """Agent 统一入口

    使用方式：
        host = AgentHost(workspace=Path.cwd())
        for chunk in host.run("写一个快速排序"):
            ...
        # 人工介入后恢复
        for chunk in host.resume_intervention("continue"):
            ...
    """

    def __init__(
        self,
        workspace: Path | None = None,
        sessions_root: Path | str | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        root = Path(sessions_root) if sessions_root else (self.workspace / session_dir)
        self.session_manager = SessionManager(sessions_root=root, workspace=self.workspace)
        logger.info("[AgentHost] initialized, workspace=%s, sessions_root=%s", self.workspace, root)

    # ============================================================
    # 属性透出
    # ============================================================

    @property
    def session(self):
        """当前活跃 Session（可能为 None）"""
        return self.session_manager.session

    # ============================================================
    # 会话生命周期
    # ============================================================

    def get_or_create_session(self, session_id: str | None = None):
        """获取/恢复/创建会话（三级优先级）"""
        return self.session_manager.get_or_create(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出磁盘上的全部会话"""
        return self.session_manager.list_sessions()

    def list_available_turns(self, session_id: str | None = None) -> list[int]:
        """列出会话可回滚的 turn"""
        return self.session_manager.list_available_turns(session_id)

    def rewind_to_turn(self, target_turn: int, session_id: str | None = None) -> dict[str, Any]:
        """回滚到指定 turn（对话 + 用户代码）"""
        return self.session_manager.rewind_to_turn(session_id, target_turn)

    # ============================================================
    # 执行
    # ============================================================

    def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        agent_mode: str = "auto",
        approval_handler: Callable[..., Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """执行一轮任务（plan → coding → valid → finally）"""
        yield from self.session_manager.stream_session_events(
            session_id=session_id,
            user_input=user_input,
            workspace=self.workspace,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
        )

    def resume_intervention(
        self,
        action: str,
        *,
        agent_mode: str = "auto",
        approval_handler: Callable[..., Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """人工介入后恢复：continue 重新规划 / stop 结束"""
        yield from self.session_manager.resume_session(
            action,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
        )

    def has_pending_intervention(self, session_id: str | None = None) -> bool:
        """是否有未处理的人工介入（含跨进程恢复场景）"""
        return self.session_manager.has_pending_intervention(session_id)
