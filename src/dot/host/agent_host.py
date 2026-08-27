"""
AgentHost — Agent 进程入口

一个 Agent 进程 = 一个工作空间 = 一组进程级组件 + 一个 Session。

进程级组件（AgentContext）：
  - MCP host（外部工具连接）
  - Skill host（Skill 发现与加载）
  - Hook runner（生命周期 Hook）
  - Permission manager（三级权限管控）
  - Tracer（链路追踪）
  - compiled graph（LangGraph 编译产物）

Session 只持有会话状态（消息、turn 状态、压缩状态、持久化），
不持有进程级组件引用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

from ..core.log import get_logger
from ..constant.session import session_dir
from ..session.agent_context import AgentContext
from ..session.manager import SessionManager
from .context_builder import ContextBuilder

logger = get_logger(__name__)


class AgentHost:
    """Agent 进程入口

    使用方式：
        host = AgentHost(workspace=Path.cwd())
        for chunk in host.run("写一个快速排序"):
            ...
        for chunk in host.resume_intervention("continue"):
            ...
    """

    def __init__(
        self,
        workspace: Path | None = None,
        sessions_root: Path | str | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        # session root 路径
        root = Path(sessions_root) if sessions_root else (self.workspace / session_dir)

        # 构建进程级组件容器（使用 ContextBuilder）
        builder = ContextBuilder(self.workspace)
        self.context = builder.build()

        # 会话管理器（单会话）
        self.session_manager = SessionManager(
            sessions_root=root,
            workspace=self.workspace,
            context=self.context,
        )
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
        """获取/恢复/创建会话"""
        return self.session_manager.get_or_create(session_id)

    def new_session(self):
        """创建全新空会话并设为活跃（/new 命令入口）"""
        return self.session_manager.new_session()

    def switch_session(self, session_id: str):
        """切换活跃会话"""
        return self.session_manager.switch_to(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出磁盘上的全部会话"""
        return self.session_manager.list_sessions()

    def list_available_turns(self, session_id: str | None = None) -> list[int]:
        """列出会话可回滚的 turn"""
        return self.session_manager.list_available_turns(session_id)

    def rewind_to_turn(self, target_turn: int, session_id: str | None = None) -> dict[str, Any]:
        """回滚到指定 turn（对话 + 用户代码）"""
        return self.session_manager.rewind_to_turn(target_turn, session_id)

    # ============================================================
    # 执行
    # ============================================================

    def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        agent_mode: str = "auto",
    ) -> Iterator[dict[str, Any]]:
        """执行一轮任务（plan → coding → valid → finally）"""
        yield from self.session_manager.stream_session_events(
            session_id=session_id,
            user_input=user_input,
            agent_mode=agent_mode,
        )

    def resume_intervention(
        self,
        action: str,
        *,
        agent_mode: str = "auto",
    ) -> Iterator[dict[str, Any]]:
        """人工介入后恢复：continue 重新规划 / stop 结束"""
        yield from self.session_manager.resume_session(
            action,
            agent_mode=agent_mode,
        )

    def has_pending_intervention(self, session_id: str | None = None) -> bool:
        """是否有未处理的人工介入"""
        return self.session_manager.has_pending_intervention(session_id)

    # ============================================================
    # CLI 桥接扩展
    # ============================================================

    def request_cancel(self, session_id: str | None = None) -> bool:
        """中断当前 turn（Ctrl+C 入口）"""
        return self.session_manager.request_cancel(session_id)

    def set_run_mode(self, mode: str, session_id: str | None = None) -> None:
        """设置 CLI 运行模式"""
        session = self.session_manager.get_or_create(session_id)
        if session is not None:
            session.run_mode = mode

    def get_run_mode(self, session_id: str | None = None) -> str:
        """读取当前运行模式"""
        try:
            session = self.session_manager.get_session(session_id)
            return getattr(session, "run_mode", "agent") if session is not None else "agent"
        except RuntimeError:
            return "agent"

    def get_token_status(self, session_id: str | None = None) -> dict[str, Any]:
        """读取当前 Token 水位与压缩统计"""
        session = self.session_manager.get_session(session_id)
        if session is None:
            return {}
        try:
            from ..compress.budget import ContextBudgetAllocator
            budget = ContextBudgetAllocator()
            current = budget.estimate_tokens(list(session.messages))
            info = budget.get_budget_info()
            cs = getattr(session, "compression_state", None)
            return {
                "current_tokens": current,
                "context_window": budget.context_window,
                "water_level": round(current / max(budget.context_window, 1) * 100, 1),
                "compression_threshold": info["compression_threshold"],
                "l1_threshold": info["l1_threshold"],
                "l2_threshold": info["l2_threshold"],
                "l3_threshold": info["l3_threshold"],
                "total_compressions": getattr(cs, "total_compressions", 0) if cs else 0,
                "total_tokens_saved": getattr(cs, "total_tokens_saved", 0) if cs else 0,
                "message_count": len(session.messages),
            }
        except Exception as exc:
            logger.debug("[AgentHost] get_token_status failed: %s", exc)
            return {"message_count": len(session.messages) if session else 0}

    def get_mcp_status(self) -> dict[str, Any]:
        """读取 MCP 连接状态"""
        host = self.context.mcp_host
        if host is None:
            return {"online": False, "servers": [], "tools": []}
        try:
            tools = host.get_all_tool_names()
            servers = []
            mgr = self.context.mcp_manager
            if mgr is not None:
                servers = mgr.list_servers()
            return {"online": len(tools) > 0, "servers": servers, "tools": tools}
        except Exception as exc:
            logger.debug("[AgentHost] get_mcp_status failed: %s", exc)
            return {"online": False, "servers": [], "tools": []}

    def restart_mcp(self) -> dict[str, Any]:
        """重启 MCP"""
        try:
            self.context.mcp_host.close()
        except Exception as exc:
            logger.debug("[AgentHost] restart_mcp close: %s", exc)
        try:
            self.context.mcp_manager.load_config_and_connect(force=True)
            self.context.mcp_host.discover_tools()
        except Exception as exc:
            logger.warning("[AgentHost] restart_mcp reconnect failed: %s", exc)
        return self.get_mcp_status()

    def save_current_session(self, name: str | None = None) -> str:
        """保存当前会话"""
        session = self.session_manager.get_session()
        if session is None:
            raise RuntimeError("No active session to save")
        if session.persistence is not None:
            from ..session.persistence import persist_turn
            persist_turn(session, session.current_turn_id or 1)
        return session.session_id

    def load_session_by_name(self, name: str):
        """加载历史会话"""
        return self.session_manager.get_or_create(name)

    # ============================================================
    # 审批配置（由 UI 层调用）
    # ============================================================

    def set_approval_handler(self, handler: Callable[[dict[str, Any]], bool] | None) -> None:
        """设置 ASK 审批回调（UI 层根据交互模式决定使用哪个 handler）

        - 控制台/TUI: 传入 console approval handler
        - 非交互 (run): 传 None，ASK 自动降级 DENY
        """
        if self.context.permission_manager is not None:
            self.context.permission_manager.set_approval_handler(handler)

    def close(self) -> None:
        """关闭进程级组件"""
        try:
            self.context.mcp_host.close()
        except Exception as exc:
            logger.debug("[AgentHost] close mcp: %s", exc)
