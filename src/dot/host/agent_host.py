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
from typing import Any, Iterator

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

        trace: 跟着session走，每个turn都有一个span，span之间有parent关系
        session的state跟着session走
        mcp ，skill，hook，权限控制 这些都是各个对话共享的
        一个workspace 的所有会话共享一个workspace，mcp ，skill，hook，权限控制 这些都是各个对话共享的
    """

    def __init__(
        self,
        workspace: Path | None = None,
        sessions_root: Path | str | None = None,
    ) -> None:
        """
        初始化 AgentHost，加载全局组件并准备会话管理。
        链路追踪，权限管控， mcp ，skill， hook，审批引擎

        :param workspace: 工作空间目录，默认当前工作目录
        :param sessions_root: 会话根目录，默认 <workspace>/.dot/sessions/
        """
        self.workspace = workspace or Path.cwd()
        root = Path(sessions_root) if sessions_root else (self.workspace / session_dir)

        # 链路追踪初始化（DOT_TRACE_ENABLED=0 关闭；落盘 <workspace>/.dot/traces/）
        from dot.trace import init_tracer

        init_tracer(self.workspace)

        # 权限系统初始化：加载 .agent-security.json + 默认控制台 Y/N 审批
        from dot.core.permission import get_permission_manager, make_console_approval_handler

        pm = get_permission_manager()
        pm.load_project(self.workspace)
        pm.set_approval_handler(make_console_approval_handler())

        # 共享设施（per-workspace，单份，全会话复用：MCP/Skill/Hook/compiled_graph）
        from dot.host.shared_services import SharedServices

        self.shared = SharedServices(self.workspace)

        self.session_manager = SessionManager(
            sessions_root=root, workspace=self.workspace, shared=self.shared
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
        """获取/恢复/创建会话（三级优先级）"""
        return self.session_manager.get_or_create(session_id)

    def new_session(self):
        """创建全新空会话并设为活跃（/new 命令入口）"""
        return self.session_manager.new_session()

    def switch_session(self, session_id: str):
        """切换活跃会话（不关闭其他）"""
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
        """是否有未处理的人工介入（含跨进程恢复场景）"""
        return self.session_manager.has_pending_intervention(session_id)

    # ============================================================
    # CLI 桥接扩展（CLI ↔ Agent Graph 适配面，仅调度，不实现业务）
    # ============================================================

    def request_cancel(self, session_id: str | None = None) -> bool:
        """中断当前 turn（Ctrl+C 入口），委托 SessionManager 取消令牌"""
        return self.session_manager.request_cancel(session_id)

    def set_run_mode(self, mode: str, session_id: str | None = None) -> None:
        """设置 CLI 运行模式（agent/chat/code），写入 session.run_mode，下一轮热生效"""
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
        """读取当前 Token 水位与压缩统计（供 TUI 状态栏展示，只读）"""
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
        """读取 MCP 连接状态（供 TUI 状态栏 / /mcp list，只读）"""
        host = self.shared.mcp_host
        if host is None:
            return {"online": False, "servers": [], "tools": []}
        try:
            tools = host.get_all_tool_names()
            servers = []
            mgr = getattr(self.shared, "mcp_manager", None)
            if mgr is not None:
                servers = mgr.list_servers()
            return {"online": len(tools) > 0, "servers": servers, "tools": tools}
        except Exception as exc:
            logger.debug("[AgentHost] get_mcp_status failed: %s", exc)
            return {"online": False, "servers": [], "tools": []}

    def restart_mcp(self) -> dict[str, Any]:
        """重启 MCP（/mcp restart）：断开重连 + 重新发现工具，不销毁当前会话"""
        try:
            self.shared.mcp_host.close()
        except Exception as exc:
            logger.debug("[AgentHost] restart_mcp close: %s", exc)
        try:
            self.shared.mcp_manager.load_config_and_connect(force=True)
            self.shared.mcp_host.discover_tools()
        except Exception as exc:
            logger.warning("[AgentHost] restart_mcp reconnect failed: %s", exc)
        return self.get_mcp_status()

    def save_current_session(self, name: str | None = None) -> str:
        """保存当前会话（/save）：触发一次 session.json 全量落盘"""
        session = self.session_manager.get_session()
        if session is None:
            raise RuntimeError("No active session to save")
        if session.persistence is not None:
            from ..session.persistence import persist_turn

            persist_turn(session, session.current_turn_id or 1)
        return session.session_id

    def load_session_by_name(self, name: str):
        """加载历史会话（/load）"""
        return self.session_manager.get_or_create(name)
