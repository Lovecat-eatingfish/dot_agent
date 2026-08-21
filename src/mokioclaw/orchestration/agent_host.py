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

from mokioclaw.core.hook_loader import load_hooks_into_runner
from mokioclaw.core.hooks import HookRunner
from mokioclaw.core.log import get_logger
from mokioclaw.orchestration.agent_authorizer import AgentAuthorizer, AutoModeClassifier
from mokioclaw.orchestration.coding_graph import (
    REPLAN_THRESHOLD,
    MAX_ATTEMPT_DEFAULT,
    Session,
    SessionManager,
    build_graph,
    build_initial_state,
)
from mokioclaw.orchestration.mcp_manager import MCPManager
from mokioclaw.orchestration.mcp_host import MCPHost
from mokioclaw.orchestration.session_persistence import SessionPersistence
from mokioclaw.reliability.git_utils import git_init
from mokioclaw.orchestration.skill_host import SkillHost
from mokioclaw.orchestration.skills_manager import SkillsManager
from mokioclaw.providers.openai_provider import create_model
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

        # Trace 管理器（全局共享）
        self._trace_mgr = TraceManager(storage=storage_provider)

        # 分类器模型（Auto 模式用，独立于主 Agent）
        self._classifier_model = classifier_model

    # ============================================================
    # Session lifecycle
    # ============================================================

    def create_session(self, session_id: Optional[str] = None) -> Session:
        """创建新会话

        - 图只编译一次
        - state 只初始化一次
        - 初始化 MCP / Skills / Authorizer / Tracing
        - 持久化目录 + git init

        Args:
            session_id: 可选指定 session ID。
                        None 则使用时间戳格式（如 20250820-153045）。
                        传入 "uuid" 显式使用 UUID。

        Returns:
            Session 对象（含 compiled_graph + current_state + 所有管理器）
        """
        if session_id == "uuid":
            sid = str(_uuid())
        elif session_id is None:
            sid = _make_timestamp_id()
        else:
            sid = session_id

        # 1. 编译图（每个 session 只编译一次）
        compiled_graph = build_graph().compile()

        # 2. 初始化 state
        initial_state = build_initial_state()

        # 3. 初始化持久化层
        persistence = SessionPersistence(sessions_root=self._sessions_root)
        persistence.save_session_meta(sid, persistence._empty_session_meta(sid))
        git_init(persistence.session_dir(sid))

        # 4. 初始化 MCP Manager + Host
        mcp_mgr = MCPManager(workspace=persistence.session_dir(sid))
        mcp_host = MCPHost(mcp_mgr)

        # 5. 初始化 Skills Manager + Host
        skills_mgr = SkillsManager(workspace=persistence.session_dir(sid))
        skill_host = SkillHost(skills_mgr)

        # 6. 初始化 Authorizer（Auto 模式分类器）
        model = self._classifier_model or create_model()
        classifier = AutoModeClassifier(model=model)
        authorizer = AgentAuthorizer(classifier=classifier)

        # 6.5 初始化 HookRunner 并加载 hooks 配置
        hook_runner = HookRunner()
        load_hooks_into_runner(hook_runner, persistence.session_dir(sid))

        # 7. 组装 Session
        session = Session(
            session_id=sid,
            compiled_graph=compiled_graph,
            current_state=initial_state,
            persistence=persistence,
            workspace=persistence.session_dir(sid),
            mcp_manager=mcp_mgr,
            mcp_host=mcp_host,
            skills_manager=skills_mgr,
            skill_host=skill_host,
            authorizer=authorizer,
            hook_runner=hook_runner,
        )

        # 8. 存入内存
        self._session_mgr._sessions[sid] = session

        logger.info("Session created: %s", sid)
        return session

    def resume_latest_session(self) -> Session:
        """恢复最新的会话

        扫描 sessions_root 目录，找到最近修改的会话，
        加载其 session.json messages 到内存，复用已有的 compiled_graph 和 state。

        Returns:
            最新会话的 Session 对象

        Raises:
            RuntimeError: 没有找到任何已有会话
        """
        # 1. 找到最新会话目录
        latest_dir = self._find_latest_session_dir()
        if latest_dir is None:
            raise RuntimeError("No existing sessions found. Create one first with create_session().")

        latest_sid = latest_dir.name

        # 2. 如果该 session 已在内存中，直接返回
        if latest_sid in self._session_mgr._sessions:
            session = self._session_mgr._sessions[latest_sid]
            # 刷新 messages（从磁盘加载最新）
            if session.persistence is not None:
                disk_meta = session.persistence.load_session_meta(latest_sid)
                disk_messages = disk_meta.get("messages", [])
                if disk_messages:
                    session.current_state["messages"] = disk_messages
            logger.info("Resumed existing session from memory: %s", latest_sid)
            return session

        # 3. 从磁盘恢复：编译图 + 加载 messages + 初始化管理器
        compiled_graph = build_graph().compile()

        persistence = SessionPersistence(sessions_root=self._sessions_root)
        disk_meta = persistence.load_session_meta(latest_sid)
        disk_messages = disk_meta.get("messages", [])

        # 恢复 current_turn_id
        current_turn_id = disk_meta.get("current_turn_id", 0)

        initial_state = build_initial_state(messages=disk_messages)
        initial_state["replan_count"] = disk_meta.get("replan_count", 0)
        initial_state["attempt_count"] = disk_meta.get("attempt_count", 0)

        # 初始化管理器
        mcp_mgr = MCPManager(workspace=persistence.session_dir(latest_sid))
        mcp_host = MCPHost(mcp_mgr)
        skills_mgr = SkillsManager(workspace=persistence.session_dir(latest_sid))
        skill_host = SkillHost(skills_mgr)
        model = self._classifier_model or create_model()
        classifier = AutoModeClassifier(model=model)
        authorizer = AgentAuthorizer(classifier=classifier)

        # 初始化 HookRunner 并加载 hooks 配置
        hook_runner = HookRunner()
        load_hooks_into_runner(hook_runner, persistence.session_dir(latest_sid))

        session = Session(
            session_id=latest_sid,
            compiled_graph=compiled_graph,
            current_state=initial_state,
            persistence=persistence,
            workspace=persistence.session_dir(latest_sid),
            mcp_manager=mcp_mgr,
            mcp_host=mcp_host,
            skills_manager=skills_mgr,
            skill_host=skill_host,
            authorizer=authorizer,
            hook_runner=hook_runner,
            current_turn_id=current_turn_id,
        )

        self._session_mgr._sessions[latest_sid] = session
        logger.info("Resumed latest session from disk: %s (turns=%d)", latest_sid, current_turn_id)
        return session

    def _find_latest_session_dir(self) -> Optional[Path]:
        """找到最近修改的会话目录"""
        root = self._sessions_root
        if not root.exists():
            return None
        candidates = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def get_session(self, session_id: str) -> Session:
        """获取会话"""
        return self._session_mgr.get_session(session_id)

    def destroy_session(self, session_id: str) -> None:
        """销毁会话（清理内存）"""
        self._session_mgr.destroy_session(session_id)

    # ============================================================
    # 运行入口
    # ============================================================

    def run(self, session_id: str, user_input: Optional[str] = None) -> Generator[dict[str, Any], None, None]:
        """驱动一轮 Agent 执行

        - 复用已有的 compiled_graph 和 current_state
        - 自动处理持久化（stream 结束后自动 diff + git commit + turn 快照）
        - 自动创建 trace 埋点

        Args:
            session_id: 会话 ID
            user_input: 用户输入（None 则复用当前 state）

        Yields:
            图执行事件字典
        """
        session = self._session_mgr.get_session(session_id)

        # 链路追踪：创建 trace
        trace = self._trace_mgr.start_trace(session_id, user_input or "")

        try:
            # 驱动图执行
            yield from self._session_mgr.stream_session_events(
                session_id,
                new_user_input=user_input,
            )
            self._trace_mgr.end_trace(trace.trace_id, "success")
        except Exception as exc:
            logger.error("Session %s run failed: %s", session_id, exc, exc_info=True)
            self._trace_mgr.end_trace(trace.trace_id, "error", str(exc))
            raise

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
