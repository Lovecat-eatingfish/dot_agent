"""
SessionManager — 单会话管理器（dot 版，自定义机制，不依赖 langgraph checkpoint）

职责：
  - 只管理一个 Session（不维护 _sessions dict）
  - get_or_create: 指定 id → 最新磁盘 → 全新
  - 初始化：编译图、MCP/Skills hosts、HookRunner、RuntimeState
  - stream_session_events: 驱动一轮图执行，yield 事件流
  - resume_session: 人工介入后恢复（continue 重新进图 / stop 结束）
  - rewind_to_turn: 磁盘 + 内存 + 用户代码一起恢复

持久化在 finally 节点内完成（session.json / turn 快照 / agent git commit），
图执行异常中断时不落 turn（无 checkpoint，靠 messages 内存 + 下一轮重跑）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import HumanMessage

from ..constant.session import session_dir
from ..core import runtime_registry
from ..core.hook_loader import load_hooks_into_runner
from ..core.hooks import HookRunner
from ..core.log import get_logger
from ..mcp.host import MCPHost
from ..mcp.manager import MCPManager
from ..core.runtime import RuntimeState
from .session import Session
from .persistence import (
    SessionPersistence,
    deserialize_messages,
)
from ..skills.host import SkillHost
from ..skills.manager import SkillsManager

# 注意：graph.coding_graph 必须延迟 import —— 它 import .session 模块，
# 顶层导入会与 session 包的 __init__ 形成循环

logger = get_logger(__name__)


class SessionManager:
    """单会话管理器

    使用方式：
        mgr = SessionManager(sessions_root=workspace / ".dot" / "sessions")
        session = mgr.get_or_create("my-session")   # 指定 ID
        session = mgr.get_or_create()               # 最新 or 新建
    """

    def __init__(self, sessions_root: Path | str | None = None, workspace: Path | None = None) -> None:
        self._sessions_root = Path(sessions_root) if sessions_root else Path(session_dir)
        self._default_workspace = workspace or Path.cwd()
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        self._session: Session | None = None

    # ============================================================
    # Public API
    # ============================================================

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def get_or_create(self, session_id: str | None = None) -> Session:
        """获取或创建会话

        优先级：
          1. 内存已有且 id 匹配（或未指定 id）→ 直接返回
          2. 指定 session_id → 从磁盘加载
          3. 未指定 id → 加载最新的磁盘 session
          4. 都没有 → 创建全新 session
        """
        if self._session is not None:
            if session_id is None or session_id == "" or self._session.session_id == session_id:
                logger.info("[SessionManager] returning existing session: %s", self._session.session_id)
                return self._session
            # 切换 session：清理旧注册
            runtime_registry.clear(self._session.session_id)
            logger.info("[SessionManager] switching from %s to %s", self._session.session_id, session_id)
            self._session = None

        target_id = session_id or None
        if target_id is None:
            latest = self._find_latest_session_dir()
            target_id = latest.name if latest is not None else self._make_timestamp_id()
            logger.info("[SessionManager] no session_id specified, using: %s", target_id)

        session = self._load_or_create_session(target_id)
        self._session = session
        logger.info("[SessionManager] session ready: %s  (turns=%d, msgs=%d, awaiting_intervention=%s)",
                     session.session_id, session.current_turn_id, len(session.messages),
                     session.awaiting_intervention)
        return session

    def get_session(self, session_id: str | None = None) -> Session:
        """获取会话（不创建新）"""
        if self._session is not None:
            if not session_id or self._session.session_id == session_id:
                return self._session
        if session_id:
            session = self._load_or_create_session(session_id)
            self._session = session
            return session
        raise RuntimeError("No active session. Call get_or_create() first.")

    def destroy_session(self) -> None:
        """清理内存"""
        if self._session is not None:
            runtime_registry.clear(self._session.session_id)
        self._session = None

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出磁盘上的所有 session 概要"""
        result = []
        if not self._sessions_root.exists():
            return result
        persistence = SessionPersistence(sessions_root=self._sessions_root)
        for session_dir in sorted(self._sessions_root.iterdir()):
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            meta = persistence.load_session_meta(session_dir.name)
            result.append({
                "session_id": session_dir.name,
                "turn_id": int(meta.get("current_turn_id", 0)),
                "task": (meta.get("task") or "")[:60],
                "messages": len(meta.get("messages", [])),
                "awaiting_intervention": bool(meta.get("awaiting_intervention", False)),
            })
        return result

    def list_available_turns(self, session_id: str | None = None) -> list[int]:
        """列出会话的所有 turn"""
        sid = session_id or (self._session.session_id if self._session else None)
        if sid is None:
            return []
        persistence = self._get_persistence(sid)
        return persistence.list_available_turns(sid)

    def rewind_to_turn(self, session_id: str | None, target_turn_id: int) -> dict[str, Any]:
        """回滚到指定 turn（磁盘 + 内存 + 用户代码一起恢复）"""
        session = self.get_session(session_id)
        if session.is_running:
            raise RuntimeError(f"Session {session.session_id} is running, cannot rewind")

        persistence = session.persistence
        graph_state, snapshot = persistence.read_turn_snapshot(session.session_id, target_turn_id)
        persistence.rewind_to_turn(session.session_id, target_turn_id, workspace=session.workspace)

        # 内存恢复：messages + per-turn 字段
        raw_messages = snapshot.get("full_messages", [])
        session.messages = deserialize_messages(raw_messages) if raw_messages else []
        session.task_plan = dict(graph_state.get("task_plan", {}))
        session.replan_count = int(graph_state.get("replan_count", 0))
        session.attempt_count = int(graph_state.get("attempt_count", 0))
        session.validate_result = dict(graph_state.get("validate_result", {}))
        session.need_human_intervene = bool(graph_state.get("need_human_intervene", False))
        session.resume_action = graph_state.get("resume_action", "")
        session.plan_invalid = bool(graph_state.get("plan_invalid", False))
        session.awaiting_intervention = bool(graph_state.get("awaiting_intervention", False))
        session.current_turn_id = target_turn_id

        logger.info("Session %s rewound to turn %d (code restored via agent git)", session.session_id, target_turn_id)
        return graph_state

    # ============================================================
    # Runtime config（写入 session.runtime）
    # ============================================================

    def apply_runtime_config(
        self,
        session: Session,
        *,
        workspace: Path | None = None,
        max_attempts: int = 3,
        approval_mode: str = "inline",
        agent_mode: str = "auto",
        approval_handler: Any = None,
    ) -> None:
        """应用运行时配置；agent_mode 联动审批策略（对齐 fix.md 工作模式）：
        - auto：approval_mode 联动为 auto（权限最大）
        - edit：approval_mode 联动为 inline（每次 bash 审批由 bash_tool 处理）
        - plan：只读工具集（build_tools_for_session 按 agent_mode 选择）
        """
        user_workspace = (
            workspace if workspace is not None
            else (session.workspace if str(session.workspace) and session.workspace.exists() else self._default_workspace)
        )
        session.workspace = user_workspace
        session.max_attempt = max_attempts

        # agent_mode → 审批策略联动（未显式指定 approval_mode 时生效）
        if approval_mode is None:
            approval_mode = "auto" if agent_mode == "auto" else "inline"

        runtime = self._ensure_runtime(session)
        runtime.workspace = user_workspace
        runtime.approval_mode = approval_mode
        runtime.agent_mode = agent_mode
        runtime.session_id = session.session_id
        if approval_handler is not None:
            runtime.approval_handler = approval_handler

    # ============================================================
    # Stream events（每轮图执行）
    # ============================================================

    def stream_session_events(
        self,
        session_id: str | None = None,
        user_input: str | None = None,
        *,
        workspace: Path | None = None,
        max_attempts: int = 3,
        approval_mode: str | None = None,
        agent_mode: str = "auto",
        approval_handler: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """驱动一轮图执行，yield 事件流

        流程：
        1. get_or_create 获取 session
        2. 应用运行时配置
        3. 重置 per-turn 字段（先重置，再追加用户输入）
        4. graph.stream({"session": session})（持久化在 finally 节点内）
        """
        session = self.get_or_create(session_id)
        if session.is_running:
            raise RuntimeError(f"Session {session.session_id} is already running")

        logger.info("[stream] session=%s, user_input=%r, agent_mode=%s", session.session_id, user_input, agent_mode)

        self.apply_runtime_config(
            session,
            workspace=workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
        )

        # 先重置，再追加用户输入
        session.reset_per_turn()
        if user_input:
            session.messages.append(HumanMessage(content=user_input))
            session.task = user_input
            logger.info("[stream] appended user input, messages=%d", len(session.messages))

        config = {"recursion_limit": 60}

        session.is_running = True
        logger.info("[stream] graph stream starting...")
        try:
            for chunk in session.compiled_graph.stream({"session": session}, config=config):
                yield chunk
        finally:
            session.is_running = False

    def resume_session(
        self,
        resume_action: str,
        *,
        session_id: str | None = None,
        agent_mode: str = "auto",
        approval_handler: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """人工介入后恢复（自定义机制，不依赖 langgraph Command）

        Args:
            resume_action:
              - "continue"：清零计数后重新进图（从 plan 重新规划）
              - "stop"：清除介入标记，本轮结束（不进图）
        """
        session = self.get_or_create(session_id)
        if session.is_running:
            raise RuntimeError(f"Session {session.session_id} is already running")
        if not session.awaiting_intervention:
            logger.warning("[resume] session %s has no pending intervention", session.session_id)
            return

        session.awaiting_intervention = False
        session.resume_action = resume_action

        if resume_action == "stop":
            # 结束：更新 session.json 里的介入标记即可
            self._persist_meta_only(session)
            logger.info("[resume] intervention stopped, session=%s", session.session_id)
            return

        # continue：计数清零，从 plan 重新规划
        session.replan_count = 0
        session.attempt_count = 0
        session.plan_invalid = False
        session.need_human_intervene = False
        session.resume_action = ""

        self.apply_runtime_config(
            session,
            agent_mode=agent_mode,
            approval_handler=approval_handler,
        )

        config = {"recursion_limit": 60}
        session.is_running = True
        logger.info("[resume] re-entering graph from plan_node")
        try:
            for chunk in session.compiled_graph.stream({"session": session}, config=config):
                yield chunk
        finally:
            session.is_running = False

    def has_pending_intervention(self, session_id: str | None = None) -> bool:
        """检查 session 是否有未处理的人工介入（含跨进程：读 session.json）"""
        session = self.get_or_create(session_id)
        if session.awaiting_intervention:
            return True
        if session.persistence is None:
            return False
        meta = session.persistence.load_session_meta(session.session_id)
        return bool(meta.get("awaiting_intervention", False))

    # ============================================================
    # Internal
    # ============================================================

    def _persist_meta_only(self, session: Session) -> None:
        """轻量更新 session.json（不清 turn，不加 turn）"""
        if session.persistence is None:
            return
        meta = session.persistence.load_session_meta(session.session_id)
        meta["awaiting_intervention"] = session.awaiting_intervention
        meta["updated_at"] = meta.get("updated_at", "")
        session.persistence.save_session_meta(session.session_id, meta)

    def _load_or_create_session(self, session_id: str) -> Session:
        """从磁盘加载或创建全新 session（共用初始化逻辑）"""
        persistence = SessionPersistence(sessions_root=self._sessions_root)
        session_path = persistence.session_dir(session_id)
        is_new = not session_path.exists()
        logger.info("[SessionManager] %s session=%s", "creating new" if is_new else "loading existing", session_id)

        meta = {} if is_new else persistence.load_session_meta(session_id)

        # MCP / Skills hosts
        workspace = Path(meta.get("workspace", "")) if meta.get("workspace") else self._default_workspace
        if not workspace.exists():
            workspace = self._default_workspace

        logger.info("[SessionManager] workspace=%s", workspace)

        mcp_host = self._build_mcp_host(workspace)
        skill_host = self._build_skill_host(workspace)

        # HookRunner
        hook_runner = HookRunner()
        try:
            load_hooks_into_runner(hook_runner, workspace)
        except Exception as exc:
            logger.debug("hook load skipped: %s", exc)

        # 图（延迟 import 断开 session↔graph 循环；无 checkpointer）
        from ..graph.coding_graph import compile_graph

        compiled_graph = compile_graph()

        session = Session(
            session_id=session_id,
            compiled_graph=compiled_graph,
            messages=deserialize_messages(meta.get("messages", [])) if meta.get("messages") else [],
            replan_count=int(meta.get("replan_count", 0)),
            attempt_count=int(meta.get("attempt_count", 0)),
            current_turn_id=int(meta.get("current_turn_id", 0)),
            persistence=persistence,
            workspace=workspace,
            mcp_host=mcp_host,
            skill_host=skill_host,
            hook_runner=hook_runner,
            awaiting_intervention=bool(meta.get("awaiting_intervention", False)),
        )

        runtime = self._ensure_runtime(session)

        # 注册运行时对象（供后续扩展/恢复挂载）
        runtime_registry.register(
            session_id,
            compiled_graph=compiled_graph,
            persistence=persistence,
            mcp_host=mcp_host,
            skill_host=skill_host,
            hook_runner=hook_runner,
            runtime=runtime,
        )

        if is_new:
            meta = persistence._empty_session_meta(session_id)
            meta["workspace"] = str(workspace)
            persistence.save_session_meta(session_id, meta)

        return session

    def _build_mcp_host(self, workspace: Path) -> MCPHost:
        """构建 MCP host（渐进披露）"""
        try:
            manager = MCPManager(workspace=workspace)
            host = MCPHost(manager)
            host.discover_tools()
            return host
        except Exception as exc:
            logger.debug("MCP host init skipped: %s", exc)
            return MCPHost(MCPManager(workspace=workspace))

    def _build_skill_host(self, workspace: Path) -> SkillHost:
        """构建 Skill host（渐进披露）"""
        try:
            manager = SkillsManager(workspace=workspace)
            host = SkillHost(manager)
            host.discover_skills()
            return host
        except Exception as exc:
            logger.debug("Skill host init skipped: %s", exc)
            return SkillHost(SkillsManager(workspace=workspace))

    def _ensure_runtime(self, session: Session) -> RuntimeState:
        if session.runtime is None:
            session.runtime = RuntimeState(
                workspace=session.workspace,
                hook_runner=session.hook_runner,
                session_id=session.session_id,
            )
        return session.runtime

    def _find_latest_session_dir(self) -> Path | None:
        """找到最近修改的会话目录"""
        if not self._sessions_root.exists():
            return None
        candidates = sorted(
            (p for p in self._sessions_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=_session_activity_time,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _get_persistence(self, session_id: str) -> SessionPersistence:
        if self._session is not None and self._session.session_id == session_id:
            if self._session.persistence is not None:
                return self._session.persistence
        return SessionPersistence(sessions_root=self._sessions_root)

    def _make_timestamp_id(self) -> str:
        from datetime import datetime

        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_id = base
        suffix = 2
        while (self._sessions_root / session_id).exists():
            session_id = f"{base}-{suffix}"
            suffix += 1
        return session_id


# ============================================================
# Helpers
# ============================================================

def _session_activity_time(session_dir: Path) -> float:
    """session 目录内最新文件修改时间"""
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
