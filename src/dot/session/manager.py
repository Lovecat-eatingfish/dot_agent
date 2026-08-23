"""
SessionManager — 多会话管理器（dot 版，自定义机制，不依赖 langgraph checkpoint）

职责：
  - 管理多个 Session（dict 索引 + 活跃指针）
  - get_or_create: 指定 id → 最新磁盘 → 全新
  - 共享设施（MCP/Skill/Hook/compiled_graph）由 SharedServices 统一初始化，
    SessionManager 只引用，不重复构建
  - stream_session_events: 驱动一轮图执行，yield 事件流
  - resume_session: 人工介入后恢复（continue 重新进图 / stop 结束）
  - rewind_to_turn: 磁盘 + 内存 + 用户代码一起恢复

持久化在 finally 节点内完成（session.json / turn 快照 / agent git commit），
图执行异常中断时不落 turn（无 checkpoint，靠 messages 内存 + 下一轮重跑）。
"""
from __future__ import annotations

import queue
import threading
import traceback as _traceback
from pathlib import Path
from typing import Any, Callable, Iterator

from langchain_core.messages import HumanMessage

from ..constant.session import session_dir
from ..core.log import get_logger
from .session import Session
from .persistence import (
    SessionPersistence,
    deserialize_messages,
)

# 注意：graph.coding_graph 必须延迟 import —— 它 import .session 模块，
# 顶层导入会与 session 包的 __init__ 形成循环

logger = get_logger(__name__)


class _RemoteException:
    """跨线程携带异常（worker 线程异常包装后塞进事件队列，主线程 re-raise）。"""

    def __init__(self, exc: BaseException, tb_str: str) -> None:
        self.exc = exc
        self.tb_str = tb_str


class SessionManager:
    """多会话管理器

    共享设施（MCP/Skill/Hook/compiled_graph）由外部 SharedServices 提供，
    SessionManager 只引用，不重复构建。

    使用方式（由 AgentHost 构造）：
        shared = SharedServices(workspace)
        mgr = SessionManager(sessions_root, workspace, shared=shared)
        session = mgr.get_or_create("my-session")
    """

    def __init__(
            self,
            sessions_root: Path | str | None = None,
            workspace: Path | None = None,
            *,
            shared: Any,
    ) -> None:
        self._sessions_root = Path(sessions_root) if sessions_root else Path(session_dir)
        self._default_workspace = workspace or Path.cwd()
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        # 多会话：dict 索引 + 活跃会话指针（切换不关闭其他）
        self._sessions: dict[str, Session] = {}
        self._active_session_id: str | None = None
        # 共享设施（来自 AgentHost.SharedServices），全会话复用
        self._shared = shared

    # ============================================================
    # Public API
    # ============================================================

    @property
    def session(self) -> Session | None:
        """当前活跃 Session（按 _active_session_id 从 dict 取）"""
        if self._active_session_id and self._active_session_id in self._sessions:
            return self._sessions[self._active_session_id]
        return None

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def get_or_create(self, session_id: str | None = None) -> Session:
        """获取/加载/创建会话（不关闭其他会话）

        优先级：
          1. 指定 id 且在内存 dict 中 → 直接返回并设为活跃
          2. 未指定 id 且已有活跃会话 → 返回活跃
          3. 指定 id 但不在内存 → 从磁盘加载入 dict
          4. 未指定 id 且无活跃 → 加载最新磁盘 / 新建
        """
        if session_id:
            # 指定 id 且在内存 dict 中 → 直接返回并设为活跃
            if session_id in self._sessions:
                self._active_session_id = session_id
                logger.info("[SessionManager] returning in-memory session: %s", session_id)
                return self._sessions[session_id]
            # 不在，可能就是 /new 创建了一个会话，需要后面创建一个session
            target_id = session_id
        else:
            # 没有传递sessionId ，到那时内存中友
            if self.session is not None:
                return self.session
            latest = self._find_latest_session_dir()
            target_id = latest.name if latest is not None else self._make_timestamp_id()
            logger.info("[SessionManager] no session_id specified, using: %s", target_id)

        session = self._load_or_create_session(target_id)
        self._sessions[session.session_id] = session
        self._active_session_id = session.session_id
        logger.info("[SessionManager] session ready: %s  (turns=%d, msgs=%d, awaiting_intervention=%s)",
                    session.session_id, session.current_turn_id, len(session.messages),
                    session.awaiting_intervention)
        return session

    def get_session(self, session_id: str | None = None) -> Session:
        """获取会话（不创建新；指定 id 不在内存则从磁盘加载，不设为活跃）"""
        if not session_id:
            session = self.session
            if session is None:
                raise RuntimeError("No active session. Call get_or_create() first.")
            return session
        if session_id in self._sessions:
            return self._sessions[session_id]
        return self._load_or_create_session(session_id)

    def new_session(self) -> Session:
        """创建全新空会话，加入 dict 并设为活跃（/new 命令入口）"""
        session_id = self._make_timestamp_id()
        session = self._load_or_create_session(session_id)
        self._sessions[session.session_id] = session
        self._active_session_id = session.session_id
        logger.info("[SessionManager] new session: %s", session.session_id)
        return session

    def switch_to(self, session_id: str) -> Session:
        """切换活跃会话（不关闭其他；不在内存则从磁盘加载）"""
        if session_id not in self._sessions:
            session = self._load_or_create_session(session_id)
            self._sessions[session_id] = session
        self._active_session_id = session_id
        return self._sessions[session_id]

    def destroy_session(self, session_id: str | None = None) -> None:
        """清理指定/活跃会话的注册项（共享设施不随会话关闭）"""
        sid = session_id or self._active_session_id
        if sid and sid in self._sessions:
            self._shutdown_session_runtime(self._sessions[sid])
            del self._sessions[sid]
            if self._active_session_id == sid:
                self._active_session_id = None

    def destroy_all(self) -> None:
        """清理全部会话的注册项"""
        for session in list(self._sessions.values()):
            self._shutdown_session_runtime(session)
        self._sessions.clear()
        self._active_session_id = None

    def _shutdown_session_runtime(self, session: Session) -> None:
        """清理 session 级资源（共享设施不随会话关闭，归 SharedServices.close）"""
        pass

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
        if not session_id:
            active = self.session
            sid = active.session_id if active else None
        else:
            sid = session_id
        if sid is None:
            return []
        persistence = self._get_persistence(sid)
        return persistence.list_available_turns(sid)

    def rewind_to_turn(self, target_turn_id: int, session_id: str | None = None) -> dict[str, Any]:
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
        # 恢复压缩状态
        from ..compress.state import CompressionState
        cs_data = graph_state.get("compression_state")
        session.compression_state = CompressionState.from_dict(cs_data) if cs_data else CompressionState()

        logger.info("Session %s rewound to turn %d (code restored via agent git)", session.session_id, target_turn_id)
        return graph_state

    # ============================================================
    # Runtime config（直接写 session 字段）
    # ============================================================

    def apply_runtime_config(
            self,
            session: Session,
            *,
            agent_mode: str = "auto",
    ) -> None:
        """应用运行时配置（权限由 PermissionManager 按 agent_mode 统一裁决）"""
        if not session.workspace or not session.workspace.exists():
            session.workspace = self._default_workspace
        session.agent_mode = agent_mode

    def request_cancel(self, session_id: str | None = None) -> bool:
        """请求中断当前 turn（CLI Ctrl+C 入口）

        设置取消令牌，worker 在下一个节点边界检测到后提前结束 graph stream。
        节点内部的长循环（如 coding_agent 多轮工具调用）需等当前节点跑完，
        不侵入 graph 节点逻辑。返回是否成功设置（会话存在且正在运行）。
        """
        session = self.get_session(session_id)
        if session is None or not session.is_running:
            return False
        session._cancel_event.set()
        logger.info("[SessionManager] cancel requested: %s", session.session_id)
        return True

    # ============================================================
    # Stream events（每轮图执行）
    # ============================================================

    def stream_session_events(
            self,
            user_input: str | None = None,
            *,
            session_id: str | None = None,
            agent_mode: str = "auto",
    ) -> Iterator[dict[str, Any]]:
        """驱动一轮图执行，yield 事件流

        流程：
        1. get_or_create 获取 session
        2. 主线程 session-state setup（apply_runtime_config / reset_per_turn / 追加用户输入）
        3. worker 线程跑 graph.stream（contextvar per-thread 隔离，trace 不串）
        4. 事件经 queue 桥接到主线程 yield（持久化在 finally 节点内）
        """
        session = self.get_or_create(session_id)
        if not session.acquire_run():
            raise RuntimeError(f"Session {session.session_id} is already running")

        logger.info("[stream] session=%s, user_input=%r, agent_mode=%s", session.session_id, user_input, agent_mode)

        # 主线程 session-state setup（worker 起来前就绪）
        self.apply_runtime_config(session, agent_mode=agent_mode)
        session.reset_per_turn()
        session._cancel_event.clear()
        if user_input:
            session.messages.append(HumanMessage(content=user_input))
            session.task = user_input
            logger.info("[stream] appended user input, messages=%d", len(session.messages))

        config = {"recursion_limit": 60}
        logger.info("[stream] graph stream starting...")

        def _body(session: Session, span: Any, emit: Callable[[Any], None]) -> None:
            cancelled = False
            for chunk in session.compiled_graph.stream({"session": session}, config=config):
                if session._cancel_event.is_set():
                    cancelled = True
                    break
                emit(chunk)
            span.set_output_summary(
                f"turn={session.current_turn_id} msgs={len(session.messages)} "
                f"passed={session.validate_result.get('passed')} awaiting={session.awaiting_intervention}"
                f" cancelled={cancelled}"
            )
            if cancelled:
                emit({"__dot_cancelled__": True})

        yield from self._run_turn_worker(
            session,
            span_name="turn",
            span_tags={"agent_mode": agent_mode, "workspace": str(session.workspace)},
            input_summary=user_input or "",
            body=_body,
        )

    def resume_session(
            self,
            resume_action: str,
            *,
            session_id: str | None = None,
            agent_mode: str = "auto",
    ) -> Iterator[dict[str, Any]]:
        """人工介入后恢复（自定义机制，不依赖 langgraph Command）

        Args:
            resume_action:
              - "continue"：清零计数后重新进图（从 plan 重新规划）
              - "stop"：清除介入标记，本轮结束（不进图，不 yield 事件）
        """
        session = self.get_or_create(session_id)
        if not session.acquire_run():
            raise RuntimeError(f"Session {session.session_id} is already running")
        if not session.awaiting_intervention:
            session.release_run()
            logger.warning("[resume] session %s has no pending intervention", session.session_id)
            return

        # 主线程 session-state setup
        session.awaiting_intervention = False
        session.resume_action = resume_action

        config = {"recursion_limit": 60}

        def _body(session: Session, span: Any, emit: Callable[[Any], None]) -> None:
            if resume_action == "stop":
                # 结束：更新 session.json 里的介入标记即可（不进图、不 emit）
                self._persist_meta_only(session)
                logger.info("[resume] intervention stopped, session=%s", session.session_id)
                span.set_output_summary("intervention stopped")
                return

            # continue：计数清零，从 plan 重新规划
            session.replan_count = 0
            session.attempt_count = 0
            session.plan_invalid = False
            session.need_human_intervene = False
            session.resume_action = ""

            self.apply_runtime_config(session, agent_mode=agent_mode)
            logger.info("[resume] re-entering graph from plan_node")
            for chunk in session.compiled_graph.stream({"session": session}, config=config):
                emit(chunk)
            span.set_output_summary(
                f"turn={session.current_turn_id} msgs={len(session.messages)} "
                f"passed={session.validate_result.get('passed')} awaiting={session.awaiting_intervention}"
            )

        yield from self._run_turn_worker(
            session,
            span_name="resume_turn",
            span_tags={"resume_action": resume_action, "agent_mode": agent_mode},
            input_summary=f"resume after intervention ({session.session_id})",
            body=_body,
        )

    def _run_turn_worker(
            self,
            session: Session,
            *,
            span_name: str,
            span_tags: dict[str, Any],
            input_summary: str,
            body: Callable[[Session, Any, Callable[[Any], None]], None],
    ) -> Iterator[dict[str, Any]]:
        """在独立工作线程跑 body，主线程从事件队列 yield。

        contextvar per-thread 隔离（trace 不串，无需碰 _begin/压栈 bug 站点）。
        调用方先 acquire_run，worker finally 里 release_run。body(session, span, emit)：
        emit(chunk) 塞事件入队；body 抛异常被捕获，包装成 _RemoteException 跨线程抛回主线程。
        """
        from ..trace import (activate_span, deactivate_span, get_tracer,
                             reset_session_context, set_session_context)

        event_queue: queue.Queue = queue.Queue()
        _SENTINEL = object()

        def _worker() -> None:
            ctx_token = set_session_context(session.session_id)
            span = get_tracer().start_span(
                "session", span_name, tags=span_tags, input_summary=input_summary,
            )
            span_token = activate_span(span)
            try:
                body(session, span, event_queue.put)
                span.finish()
            except BaseException as exc:
                span.set_output_summary(f"{type(exc).__name__}: {exc}")
                span.finish(exc)
                event_queue.put(_RemoteException(exc, _traceback.format_exc()))
            finally:
                try:
                    deactivate_span(span_token)
                except Exception:
                    pass
                reset_session_context(ctx_token)
                session.release_run()
                event_queue.put(_SENTINEL)

        thread = threading.Thread(
            target=_worker, daemon=True, name=f"dot-turn-{session.session_id}"
        )
        thread.start()

        while True:
            item = event_queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, _RemoteException):
                raise item.exc from None
            yield item

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

        workspace = Path(meta.get("workspace", "")) if meta.get("workspace") else self._default_workspace
        if not workspace.exists():
            workspace = self._default_workspace

        logger.info("[SessionManager] workspace=%s", workspace)

        # 共享设施来自 SharedServices（单份，全会话复用，不在此重复构建）
        shared = self._shared

        # 恢复压缩状态
        from ..compress.state import CompressionState
        cs_data = meta.get("compression_state")
        compression_state = CompressionState.from_dict(cs_data) if cs_data else CompressionState()

        session = Session(
            session_id=session_id,
            compiled_graph=shared.compiled_graph,
            messages=deserialize_messages(meta.get("messages", [])) if meta.get("messages") else [],
            replan_count=int(meta.get("replan_count", 0)),
            attempt_count=int(meta.get("attempt_count", 0)),
            current_turn_id=int(meta.get("current_turn_id", 0)),
            persistence=persistence,
            workspace=workspace,
            mcp_host=shared.mcp_host,
            skill_host=shared.skill_host,
            hook_runner=shared.hook_runner,
            awaiting_intervention=bool(meta.get("awaiting_intervention", False)),
            compression_state=compression_state,
            run_mode=meta.get("run_mode", "agent"),
        )

        if is_new:
            meta = persistence._empty_session_meta(session_id)
            meta["workspace"] = str(workspace)
            persistence.save_session_meta(session_id, meta)

        return session

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
        session = self._sessions.get(session_id)
        if session is not None and session.persistence is not None:
            return session.persistence
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
