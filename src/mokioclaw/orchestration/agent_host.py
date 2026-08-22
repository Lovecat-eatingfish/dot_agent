"""
Agent Host — 项目统一入口

职责：
- 项目启动时初始化一次，加载 MCP / Skills / Tracing
- 创建会话时，图只编译一次，state 只初始化一次
- 同一个会话内，图和 state 全程复用
- 支持时间格式 session_id（年月日时分秒）
- 支持 resume_latest_session() 加载最新会话的 messages

对外 API：
    host = AgentHost()
    session = host.create_session()                        # 新会话（UUID）
    session = host.create_session("20250820-153045")       # 时间格式 ID
    session = host.resume_latest_session()                  # 恢复最新会话
    for event in host.run(session.session_id, "用户输入"):
        handle(event)
    host.rewind_to_turn(session.session_id, 1)
    host.destroy_session(session.session_id)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Generator, Optional

from mokioclaw.core.log import get_logger
from mokioclaw.orchestration.coding_graph import (
    SessionManager, Session,
)
from mokioclaw.orchestration.session_persistence import _deserialize_messages
from mokioclaw.reliability.tracing import TraceManager

logger = get_logger(__name__)


# ============================================================
# AgentHost
# ============================================================

class AgentHost:
    """Agent 统一入口

    项目启动时初始化一次，后续创建多个会话。
    每个会话拥有独立的：
      - compiled_graph（图只编译一次）
      - current_state（state 只初始化一次，后续复用）
      - mcp_host / skill_host（渐进披露，有状态）
      - authorizer / hook_runner

    使用方式：
        host = AgentHost()
        session = host.create_session("my-session")
        for event in host.run(session.session_id, "写一个 hello world"):
            handle(event)
        host.rewind(session.session_id, 1)
        host.destroy_session(session.session_id)
    """

    def __init__(
            self,
            sessions_root: Optional[Path] = None,
            *,
            classifier_model: Any = None,
            storage_provider: Any = None,
    ) -> None:
        """初始化 AgentHost

        Args:
            sessions_root: 会话数据根目录，默认 .agent_sessions
            classifier_model: Auto 模式独立分类器模型（None 则用 create_model()），就是校验bash 是不是安全的那个模型
            storage_provider: Trace 存储实现（None 则用默认 FileStorageProvider）
        """
        self._sessions_root = Path(sessions_root) if sessions_root else Path(".agent_sessions")
        self._sessions_root.mkdir(parents=True, exist_ok=True)

        # 全局单例 SessionManager： 这个是核心： 里面哟mcp skill state 等的管理者
        self._session_mgr = SessionManager(sessions_root=self._sessions_root)
        # 当前交互选中的 session。SessionManager 内部字典保存所有已加载 session。
        latest = self._find_latest_session_dir()
        self._active_session_id: Optional[str] = latest.name if latest is not None else None

        # Trace 管理器（全局共享）
        self._trace_mgr = TraceManager(storage=storage_provider)

        # 分类器模型（Auto 模式用，独立于主 Agent）
        self._classifier_model = classifier_model

    # ============================================================
    # Session lifecycle
    # ============================================================

    def create_session(self, session_id: Optional[str] = None) -> Session:
        """创建新会话（委托给 SessionManager，统一初始化逻辑）

        Args:
            session_id: 可选指定 session ID。
                        None 则使用时间戳格式（如 20250820-153045）。
                        传入 "uuid" 显式使用 UUID。

        Returns:
            Session 对象（含 compiled_graph + current_state + 所有管理器）
        """
        if session_id is None:
            base_sid = _make_timestamp_id()
            sid = base_sid
            suffix = 2
            while sid in self._session_mgr._sessions or (self._sessions_root / sid).exists():
                sid = f"{base_sid}-{suffix}"
                suffix += 1
        else:
            sid = session_id

        session = self._session_mgr.create_session(sid)
        self._active_session_id = session.session_id
        return session

    @property
    def sessions(self) -> dict[str, Session]:
        """返回进程内已加载的 session 映射。"""
        return self._session_mgr._sessions

    def resume_session(self, session_id: str) -> Session:
        """加载并选中指定 session。

        指定的 session 不存在时直接报错，避免静默切换到另一个会话。
        """
        sid = str(session_id).strip()
        if not sid:
            raise ValueError("session_id must not be empty")

        session_path = self._sessions_root / sid
        if sid not in self._session_mgr._sessions and not session_path.is_dir():
            raise RuntimeError(f"Session not found: {sid}")

        session = self._session_mgr.get_session(sid)
        self._refresh_session_from_disk(session)
        self._active_session_id = sid
        logger.info("Resumed session: %s", sid)
        return session

    def resume_latest_session(self) -> Session:
        """恢复最新的会话

        warmup 已在初始化时将磁盘上的 session 加载到内存，
        这里只需找到最近修改的 session 目录，返回对应 session。

        Returns:
            最新会话的 Session 对象

        Raises:
            RuntimeError: 没有找到任何已有会话
        """
        latest_dir = self._find_latest_session_dir()
        if latest_dir is None:
            raise RuntimeError("No existing sessions found. Create one first with create_session().")

        return self.resume_session(latest_dir.name)

    def _find_latest_session_dir(self) -> Optional[Path]:
        """找到最近修改的会话目录"""
        root = self._sessions_root
        if not root.exists():
            return None
        candidates = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=self._session_activity_time,
            reverse=True,
        )
        return candidates[0] if candidates else None

    @staticmethod
    def _session_activity_time(session_dir: Path) -> float:
        """根据 session 目录内最近变更的文件判断最近活跃会话。

        只看 session 目录本身会漏掉后续 turn 文件和 Git 提交，
        因为这些变化通常发生在子目录中。
        """
        latest = 0.0
        try:
            latest = session_dir.stat().st_mtime
            for path in session_dir.rglob("*"):
                try:
                    latest = max(latest, path.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return 0.0
        return latest

    @staticmethod
    def _refresh_session_from_disk(session: Session) -> None:
        """把磁盘 session 元数据恢复到常驻内存 state。"""
        if session.persistence is None:
            return

        meta = session.persistence.load_session_meta(session.session_id)
        raw_messages = meta.get("messages", [])
        if raw_messages:
            session.current_state["messages"] = _deserialize_messages(raw_messages)
        session.current_turn_id = int(meta.get("current_turn_id", session.current_turn_id or 0))
        for key in ("replan_count", "attempt_count"):
            if key in meta:
                session.current_state[key] = meta[key]

    def run(
            self,
            user_input: Optional[str] = None,
            *,
            session_id: Optional[str] = None,
            resume_session_id: Optional[str] = None,
            workspace: Optional[Path] = None, # 工作空间
            max_attempts: int = 3,
            approval_mode: str = "inline", # 审批模式
            agent_mode: str = "auto", # agent 模式 todo：可能和approval_mode 冲突了
            approval_handler: Any = None, # 人工审批函数
            trace_mode: str = "on",
            safe_mode: bool = False,
    ) -> Generator[dict[str, Any], None, None]:
        """驱动一轮 Agent 执行（统一入口）

        - 创建新 session 或恢复已有 session
        - 自动处理持久化（stream 结束后自动 diff + git commit + turn 快照）
        - 自动创建 trace 埋点

        Args:
            user_input: 用户输入
            session_id: 指定 session ID（None 则自动生成或恢复最新）
            workspace: 工作区路径
            max_attempts: 最大重试次数
            approval_mode: 审批模式
            agent_mode: Agent 模式
            approval_handler: 审批回调
            resume_session_id: 恢复指定 session
            trace_mode: 追踪模式
            safe_mode: 安全模式

        Yields:
            图执行事件字典
            :param checkpoint_mode:
        """
        # 1. 选择当前 session：显式恢复 > 显式 session > 当前内存 session > 最近 session > 新建
        if resume_session_id:
            session = self.resume_session(resume_session_id)
        elif session_id:
            if session_id in self._session_mgr._sessions:
                session = self._session_mgr.get_session(session_id)
                self._active_session_id = session.session_id
            else:
                session = self.resume_session(session_id)
        elif self._active_session_id and self._active_session_id in self._session_mgr._sessions:
            session = self._session_mgr.get_session(self._active_session_id)
        else:
            latest = self._find_latest_session_dir()
            session = self.resume_session(latest.name) if latest is not None else self.create_session()

        session_id = session.session_id

        # 2. 注入运行时配置到 state
        _apply_runtime_config(
            session,
            workspace=workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
            checkpoint_mode=checkpoint_mode,
            trace_mode=trace_mode,
            safe_mode=safe_mode,
        )

        # 3. 链路追踪
        trace = self._trace_mgr.start_trace(session_id, user_input or "") if trace_mode != "off" else None
        if trace is not None:
            session.current_state["trace_id"] = trace.trace_id

        trace_status = "success"
        trace_error: Optional[str] = None
        try:
            # 4. 驱动图执行
            yield from self._session_mgr.stream_session_events(
                session_id,
                new_user_input=user_input,
            )
        except Exception as exc:
            logger.error("Session %s run failed: %s", session_id, exc, exc_info=True)
            trace_status = "error"
            trace_error = str(exc)
            raise
        finally:
            if trace is not None:
                self._trace_mgr.end_trace(trace.trace_id, trace_status, trace_error)

    def get_session(self, session_id: str) -> Session:
        """获取会话"""
        return self._session_mgr.get_session(session_id)

    # ============================================================
    # 运行入口
    # ============================================================

    def destroy_session(self, session_id: str) -> None:
        """销毁会话（清理内存）"""
        self._session_mgr.destroy_session(session_id)
        if self._active_session_id == session_id:
            self._active_session_id = None

    # ============================================================
    # Rewind
    # ============================================================

    def rewind_to_turn(self, session_id: str, target_turn_id: int) -> dict[str, Any]:
        """回滚到指定 turn

        步骤：
        1. git reset --hard
        2. 恢复 session.json messages
        3. 恢复内存 current_state
        4. 重置 is_running 锁
        """
        # 确保会话不在运行中
        session = self._session_mgr.get_session(session_id)
        if session.is_running:
            raise RuntimeError(f"Session {session_id} is running, cannot rewind")

        graph_state = self._session_mgr.rewind_to_turn(session_id, target_turn_id)
        session.current_turn_id = target_turn_id

        logger.info("Session %s rewound to turn %d", session_id, target_turn_id)
        return graph_state

    # ============================================================
    # 便捷方法
    # ============================================================

    def list_turns(self, session_id: str) -> list[int]:
        """列出会话的所有 turn"""
        return self._session_mgr.list_available_turns(session_id)

    def get_trace_tree(self, trace_id: str) -> Any:
        """获取 trace 树结构（可视化用）"""
        return self._trace_mgr.get_trace_tree(trace_id)

    def set_agent_mode(self, session_id: str, mode: str) -> None:
        """设置会话运行模式（Plan / Default / AcceptEdits / Auto）"""
        session = self._session_mgr.get_session(session_id)
        if session.authorizer:
            session.authorizer.set_mode(session_id, mode)

    def get_agent_mode(self, session_id: str) -> str:
        """获取当前会话运行模式"""
        session = self._session_mgr.get_session(session_id)
        if session.authorizer:
            return session.authorizer.get_mode(session_id)
        return "default"

    def authorize_tool_call(self, session_id: str, tool_name: str, args: dict[str, Any] | None = None) -> Any:
        """对工具调用做权限审批"""
        session = self._session_mgr.get_session(session_id)
        if session.authorizer:
            return session.authorizer.authorize_tool_call(session_id, tool_name, args)
        from mokioclaw.orchestration.agent_authorizer import AgentAuthorizer, AuthDecision
        default_auth = AgentAuthorizer()
        return default_auth.authorize_tool_call(session_id, tool_name, args)

    def discover_mcp_tools(self, session_id: str) -> list[str]:
        """发现 MCP 工具（渐进披露）"""
        session = self._session_mgr.get_session(session_id)
        if session.mcp_host:
            return session.mcp_host.discover_tools()
        return []

    def discover_skills(self, session_id: str) -> list[str]:
        """发现 Skills（渐进披露）"""
        session = self._session_mgr.get_session(session_id)
        if session.skill_host:
            return session.skill_host.discover_skills()
        return []


# ============================================================
# 全局单例
# ============================================================

_host: Optional[AgentHost] = None


def get_agent_host(**kwargs) -> AgentHost:
    """获取全局 AgentHost 单例"""
    global _host
    if _host is None:
        _host = AgentHost(**kwargs)
    return _host


def init_agent_host(**kwargs) -> AgentHost:
    """初始化全局 AgentHost（强制重建）"""
    global _host
    _host = AgentHost(**kwargs)
    return _host


# ============================================================
# Runtime config helper
# ============================================================

def _apply_runtime_config(
        session: Session,
        *,
        workspace: Optional[Path] = None,
        max_attempts: int = 3,
        approval_mode: str = "inline",
        agent_mode: str = "auto",
        approval_handler: Any = None,
        checkpoint_mode: str = "light",
        trace_mode: str = "on",
        safe_mode: bool = False,
) -> None:
    """将运行时配置写入 session.current_state 和 session 属性。

    workspace 是用户编码目录：用户在哪打开 agent 就自动是哪个目录（Path.cwd()）。
    所有工具执行、MCP/Skills/Hook 配置加载都在这个目录下进行。
    同时同步更新 SessionPersistence 的存储根目录（workspace/.agent_sessions/）。
    """
    # workspace 默认取当前目录（用户打开 agent 的位置）
    user_workspace = workspace if workspace is not None else Path.cwd()
    session.workspace = user_workspace
    session.current_state["workspace"] = user_workspace
    # 同步更新 persistence 的存储根目录，确保内部存储落在用户编码空间下
    if session.persistence is not None:
        session.persistence.update_workspace(user_workspace)
    session.current_state["approval_mode"] = approval_mode
    session.current_state["agent_mode"] = agent_mode
    session.max_attempt = max_attempts
    session.current_state["max_attempt"] = max_attempts
    if approval_handler is not None:
        session.current_state["approval_handler"] = approval_handler
    session.current_state["checkpoint_mode"] = checkpoint_mode
    session.current_state["trace_mode"] = trace_mode
    session.current_state["safe_mode"] = safe_mode


# ============================================================
# UUID / Timestamp helpers
# ============================================================

def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


def _make_timestamp_id() -> str:
    """生成年月日时分秒格式的 session ID，如 20250820-153045"""
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")

# ============================================================
# AgentHost
# ============================================================
