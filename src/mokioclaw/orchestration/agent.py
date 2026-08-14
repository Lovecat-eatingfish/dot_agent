from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from langgraph.graph import add_messages

from mokioclaw.reliability.checkpoint import CheckpointManager, load_resume_inputs, normalize_checkpoint_mode
from mokioclaw.core.hook_loader import load_hooks_into_runner
from mokioclaw.core.hooks import HookEvent, HookPayload, HookRunner, fire_session_hook, fire_stop_hook
from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_workspace, new_task_workspace
from mokioclaw.core.workspace_detection import resolve_workspace
from mokioclaw.tools.bash_tool import cleanup_background_processes, cleanup_old_outputs
from mokioclaw.reliability.session import (
    append_assistant_turn,
    append_user_turn,
    build_session_context,
    load_or_create_session,
    save_session,
    session_started_event,
    session_turn_saved_event,
    session_turn_started_event,
)
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.reliability.trace import TraceRecorder, normalize_trace_mode
from mokioclaw.orchestration.workflow import build_complex_workflow, build_entry_workflow
from mokioclaw.prompts.builder import reset_prompt_builder
from mokioclaw.prompts.thinking import apply_thinking_mode

logger = get_logger(__name__)

def create_runtime(
    workspace: Path | None = None,
    *,
    approval_mode: str = "inline",
    agent_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_from: Path | None = None,
    trace_mode: str | None = None,
    opened_file: Path | None = None,
    fire_session_start: bool = True,
) -> RuntimeState:
    # 智能工作区解析：显式指定 / 打开文件 → resolve_workspace，否则生成唯一 workspace
    if workspace is not None or opened_file is not None:
        selected = resolve_workspace(
            user_specified=workspace,
            opened_file=opened_file,
            fallback=resume_from,
        )
    else:
        selected = new_task_workspace()

    selected.mkdir(parents=True, exist_ok=True)

    # 启动时清理旧输出文件（非阻塞，失败不影响启动）
    try:
        cleanup_old_outputs(selected)
    except Exception as exc:
        logger.debug("output cleanup skipped: %s", exc)

    # agent_mode：显式参数 > 环境变量 > 用户配置 > auto
    resolved_agent_mode = agent_mode or os.getenv("MOKIO_AGENT_MODE") or ""
    if not resolved_agent_mode:
        try:
            from mokioclaw.config.loader import load_user_config
            resolved_agent_mode = load_user_config(selected).agent_mode
        except Exception:
            resolved_agent_mode = "auto"

    runtime = RuntimeState(
        workspace=selected,
        approval_mode=approval_mode,
        agent_mode=resolved_agent_mode,
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int("MOKIO_BASH_DEFAULT_TIMEOUT_SECONDS", 120),
        bash_max_timeout_seconds=_env_int("MOKIO_BASH_MAX_TIMEOUT_SECONDS", 600),
        bash_max_output_chars=_env_int("MOKIO_BASH_MAX_OUTPUT_CHARS", 6000),
        bash_env_file=_env_path("MOKIO_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(checkpoint_mode or os.getenv("MOKIO_CHECKPOINT_MODE", "light")),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(trace_mode or os.getenv("MOKIO_TRACE_MODE", "on")),
    )

    # 加载用户/项目 hooks.json，并触发 SessionStart
    try:
        load_hooks_into_runner(runtime.hook_runner, selected)
    except Exception as exc:
        logger.debug("hook load skipped: %s", exc)

    if fire_session_start:
        result = fire_session_hook(
            runtime.hook_runner,
            HookEvent.SessionStart,
            workspace=str(selected),
        )
        # SessionStart stdout 注入上下文（对齐 Claude Code）
        if result.context_injection:
            runtime.session_context_injection = result.context_injection

    return runtime


def _fire_session_start_once(runtime: RuntimeState) -> None:
    """触发 SessionStart 钩子并把 stdout 注入上下文（chat-only 会话也要触发，#9）

    单次 CLI 路径 stream_agent_events 在 create_runtime 后调用此函数，
    覆盖 chat-only 会话原本提前 return 导致 SessionStart 不触发的问题。
    """
    try:
        result = fire_session_hook(
            runtime.hook_runner,
            HookEvent.SessionStart,
            workspace=str(runtime.workspace),
        )
        if result.context_injection:
            runtime.session_context_injection = result.context_injection
    except Exception as exc:  # noqa: BLE001
        logger.debug("SessionStart hook fire skipped: %s", exc)


def stream_agent_events(
    task: str | None = None,
    *,
    workspace: Path | None = None,
    opened_file: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    agent_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> Iterator[dict[str, Any]]:
    cleaned_task, thinking_instruction = apply_thinking_mode(task or "")
    task = cleaned_task or task

    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None

    # UserPromptSubmit hook：可注入上下文或阻断
    _prompt_hook_runner = HookRunner()
    try:
        load_hooks_into_runner(_prompt_hook_runner, resume_path or workspace)
    except Exception as exc:
        logger.debug("hook load for UserPromptSubmit skipped: %s", exc)
    _prompt_result = _prompt_hook_runner.run(
        HookEvent.UserPromptSubmit,
        HookPayload(
            event=HookEvent.UserPromptSubmit,
            user_prompt=task or "",
            workspace=str(resume_path or workspace or ""),
        ),
    )
    if _prompt_result.blocked:
        yield {
            "type": "custom_event",
            "event": {
                "type": "prompt_blocked",
                "reason": _prompt_result.feedback or "blocked by UserPromptSubmit hook",
            },
        }
        return
    if _prompt_result.context_injection:
        task = f"{_prompt_result.context_injection}\n\n{task}"
    if resume_path is None:
        route = "workflow"
        entry_state: dict[str, Any] = {"task": task or "", "messages": []}
        # 判断简约的意图： 聊天 / plan， 如果是聊天 调用llm 返回数据 ——》end， 如果是plan直接end（后米构建更复杂的图： build_complex_workflow）
        for mode, event in build_entry_workflow().stream(entry_state, stream_mode=["updates", "custom"]):
            if mode == "custom":
                yield {"type": "custom_event", "event": event}
                if isinstance(event, dict) and event.get("type") == "intent_decision":
                    route = str(event.get("route") or "workflow")
            else:
                _merge_graph_update(entry_state, event)
                yield {"type": "graph_event", "event": event}
        if route == "chat":
            # chat-only 会话也要触发 SessionStart（原本提前 return 跳过，#9）：
            # chat_responder_node 在 entry workflow 内已用临时 runtime 构建 prompt，
            # SessionStart 注入的 context 无法回灌那条路径，这里至少保证钩子副作用
            #（如加载会话级配置、记录日志）在 chat 场景也执行一次。
            try:
                _chat_runtime = create_runtime(
                    workspace=workspace,
                    approval_mode=approval_mode,
                    agent_mode=agent_mode,
                    approval_handler=approval_handler,
                    checkpoint_mode=checkpoint_mode,
                    trace_mode=trace_mode,
                    fire_session_start=False,
                )
                _fire_session_start_once(_chat_runtime)
                # chat 场景的 SessionEnd 也应配对触发
                fire_session_hook(_chat_runtime.hook_runner, HookEvent.SessionEnd, workspace=str(_chat_runtime.workspace))
            except Exception as exc:  # noqa: BLE001
                logger.debug("chat-only session hook skipped: %s", exc)
            return

    state = create_runtime(
        workspace=resume_path or workspace,
        opened_file=opened_file,
        approval_mode=approval_mode,
        agent_mode=agent_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
        fire_session_start=False,
    )
    if thinking_instruction:
        state.thinking_instruction = thinking_instruction
    _apply_workspace_runtime_flags(state)
    # SessionStart 钩子：单次 CLI 路径在此触发（chat-only 会话原本提前 return 跳过 #9）
    _fire_session_start_once(state)
    # 每个新任务重置 PromptBuilder，确保使用当前 workspace 的配置
    reset_prompt_builder()

    # 先跑入口流程图 build_entry_workflow 做意图识别；
    # 实时推送 graph_event /custom_event；
    # 若意图判定为 chat 直接终止，不执行复杂工作流；
    workflow = build_complex_workflow()
    yield {"type": "workspace", "path": str(state.workspace)}

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        # 调用 load_resume_inputs 读取快照恢复任务上下文，产出恢复事件并推送；
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    current_state: dict[str, Any] = dict(inputs)
    # 初始化流程输入 state，创建快照管理器 CheckpointManager、追踪记录器 TraceRecorder；
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    # 任务启动时保存初始快照、记录追踪事件；
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(current_state, status="started", latest_node="start")
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

        # 流式执行复杂业务流程图 build_complex_workflow，循环处理两类事件：
        # custom 自定义事件（工具调用、意图判定、压缩事件等）：判断是否需要快照，存盘并推送事件；
        # graph 状态更新事件：合并全局 state、记录追踪、自动保存快照、推送事件；
    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        cleanup_background_processes()
        fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))
        saved = manager.save(current_state, status="interrupted", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="interrupted", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return
    except Exception as exc:
        cleanup_background_processes()
        fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))
        logger.error("workflow failed: %s", exc, exc_info=True)
        saved = manager.save(current_state, status="failed", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="failed", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    cleanup_background_processes()
    # Stop hook：模型本轮结束（对齐 Claude Code Stop）
    fire_stop_hook(
        state.hook_runner,
        workspace=str(state.workspace),
        session_id=str(getattr(state, "trace_id", "") or ""),
    )
    fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))
    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(status="finished", latest_node=latest_node, final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


def stream_session_events(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> Iterator[dict[str, Any]]:
    workspace = (resume_workspace or session_workspace or default_workspace()).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    session = load_or_create_session(workspace)
    resumed = resume_workspace is not None
    yield {"type": "custom_event", "event": session_started_event(workspace, session, resumed=resumed)}
    yield {"type": "workspace", "path": str(workspace)}

    if not task:
        return

    cleaned_task, thinking_instruction = apply_thinking_mode(task)
    task = cleaned_task or task

    # UserPromptSubmit hook：可注入上下文或阻断（TUI 持久会话）
    _prompt_hook_runner = HookRunner()
    try:
        load_hooks_into_runner(_prompt_hook_runner, workspace)
    except Exception as exc:
        logger.debug("hook load for UserPromptSubmit skipped: %s", exc)
    _prompt_result = _prompt_hook_runner.run(
        HookEvent.UserPromptSubmit,
        HookPayload(
            event=HookEvent.UserPromptSubmit,
            user_prompt=task,
            workspace=str(workspace),
            session_id=str(session.get("session_id", "")),
        ),
    )
    if _prompt_result.blocked:
        yield {
            "type": "custom_event",
            "event": {
                "type": "prompt_blocked",
                "reason": _prompt_result.feedback or "blocked by UserPromptSubmit hook",
            },
        }
        return
    if _prompt_result.context_injection:
        task = f"{_prompt_result.context_injection}\n\n{task}"

    # 显式「记住：...」同步写入记忆
    try:
        from mokioclaw.memory.auto_memory import maybe_write_explicit_memory, trigger_autodream_if_needed
        mem_result = maybe_write_explicit_memory(workspace, task)
        if mem_result and mem_result.get("ok"):
            yield {
                "type": "custom_event",
                "event": {"type": "memory_write", "path": mem_result.get("path"), "name": mem_result.get("name")},
            }
        trigger_autodream_if_needed(workspace)
    except Exception as exc:
        logger.debug("auto memory hook skipped: %s", exc)

    turn = append_user_turn(session, task)
    save_session(workspace, session)
    yield {"type": "custom_event", "event": session_turn_started_event(workspace, session, turn=turn, task=task)}
    session_context = build_session_context(workspace, session)

    route = "workflow"
    entry_state: dict[str, Any] = {
        "task": task or "",
        "messages": [],
        "session_id": session.get("session_id", ""),
        "session_turn": turn,
        "session_context": session_context,
    }

    for mode, event in build_entry_workflow().stream(entry_state, stream_mode=["updates", "custom"]):
        if mode == "custom":
            yield {"type": "custom_event", "event": event}
            if isinstance(event, dict) and event.get("type") == "intent_decision":
                route = str(event.get("route") or "workflow")
        else:
            _merge_graph_update(entry_state, event)
            yield {"type": "graph_event", "event": event}

    if route == "chat":
        response = str(entry_state.get("chat_response") or entry_state.get("final_answer") or "")
        append_assistant_turn(session, turn=turn, route="chat", content=response, summary=response)
        save_session(workspace, session)
        yield {"type": "custom_event", "event": session_turn_saved_event(workspace, session, turn=turn, route="chat")}
        return

    workflow_events = _stream_complex_workflow(
        task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
        session=session,
        turn=turn,
        session_context=session_context,
        thinking_instruction=thinking_instruction,
    )
    final_answer = ""
    for event in workflow_events:
        final_answer = _final_answer_from_event(event) or final_answer
        yield event

    append_assistant_turn(session, turn=turn, route="workflow", content=final_answer, summary=final_answer)
    save_session(workspace, session)
    yield {"type": "custom_event", "event": session_turn_saved_event(workspace, session, turn=turn, route="workflow")}

    # 后台记忆提取（不阻塞）
    try:
        from mokioclaw.memory.auto_memory import trigger_background_extraction
        trigger_background_extraction(
            workspace,
            new_messages=[f"user: {task}", f"assistant: {final_answer}"],
            session_id=str(session.get("session_id", "")),
        )
    except Exception as exc:
        logger.debug("background extraction skipped: %s", exc)


def _stream_complex_workflow(
    *,
    task: str | None,
    workspace: Path,
    max_attempts: int,
    approval_mode: str,
    approval_handler,
    checkpoint_mode: str | None,
    resume_workspace: Path | None,
    trace_mode: str | None,
    session: dict[str, Any] | None = None,
    turn: int | None = None,
    session_context: str = "",
    thinking_instruction: str = "",
) -> Iterator[dict[str, Any]]:
    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None
    # TUI 多轮：每 turn 重建 runtime，但不重复 SessionStart
    state = create_runtime(
        workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
        fire_session_start=False,
    )
    if thinking_instruction:
        state.thinking_instruction = thinking_instruction
    _apply_workspace_runtime_flags(state)

    # 会话级 SessionStart：整段 session 只触发一次
    if session is not None and not session.get("_session_hooks_started"):
        fire_session_hook(
            state.hook_runner,
            HookEvent.SessionStart,
            workspace=str(state.workspace),
            session_id=str(session.get("session_id", "")),
        )
        session["_session_hooks_started"] = True
        session.pop("_session_hooks_ended", None)
        try:
            save_session(workspace, session)
        except Exception:
            pass

    # 每个新任务重置 PromptBuilder，确保使用当前 workspace 的配置
    reset_prompt_builder()
    workflow = build_complex_workflow()

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    if session is not None:
        inputs["session_id"] = session.get("session_id", "")
    if turn is not None:
        inputs["session_turn"] = turn
    if session_context:
        inputs["session_context"] = session_context
    metadata = dict(inputs.get("metadata", {}))
    if session is not None:
        metadata["session_id"] = session.get("session_id", "")
    if turn is not None:
        metadata["session_turn"] = turn
    if metadata:
        inputs["metadata"] = metadata

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(current_state, status="started", latest_node="start")
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        cleanup_background_processes()
        saved = manager.save(current_state, status="interrupted", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="interrupted", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return
    except Exception as exc:
        cleanup_background_processes()
        logger.error("workflow failed: %s", exc, exc_info=True)
        saved = manager.save(current_state, status="failed", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="failed", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    cleanup_background_processes()
    fire_stop_hook(
        state.hook_runner,
        workspace=str(state.workspace),
        session_id=str((session or {}).get("session_id", "")),
    )
    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(status="finished", latest_node=latest_node, final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


def end_persistent_session_hooks(workspace: Path | None) -> None:
    """TUI / 多轮会话结束时触发 SessionEnd（仅当曾发过 SessionStart）

    在 /new、TUI 退出时调用；单次 CLI 流式任务仍由 stream_agent_events 自行收尾。
    """
    if workspace is None:
        return
    workspace = workspace.expanduser()
    try:
        from mokioclaw.reliability.session import load_or_create_session, session_file

        if not session_file(workspace).exists():
            return
        session = load_or_create_session(workspace)
    except Exception as exc:
        logger.debug("session load for SessionEnd skipped: %s", exc)
        return

    if not session.get("_session_hooks_started") or session.get("_session_hooks_ended"):
        return

    from mokioclaw.core.hooks import HookRunner

    runner = HookRunner()
    try:
        load_hooks_into_runner(runner, workspace)
    except Exception as exc:
        logger.debug("hook load for SessionEnd skipped: %s", exc)

    fire_session_hook(
        runner,
        HookEvent.SessionEnd,
        workspace=str(workspace),
        session_id=str(session.get("session_id", "")),
    )
    session["_session_hooks_ended"] = True
    session["_session_hooks_started"] = False
    try:
        save_session(workspace, session)
    except Exception as exc:
        logger.debug("session save after SessionEnd skipped: %s", exc)


def _apply_workspace_runtime_flags(runtime: RuntimeState) -> None:
    """读取 workspace 下的 mode / compact 标记文件"""
    root = runtime.workspace / ".mokioclaw"
    mode_file = root / "agent_mode"
    if mode_file.exists():
        try:
            runtime.agent_mode = mode_file.read_text(encoding="utf-8").strip() or runtime.agent_mode
        except OSError:
            pass
    compact_flag = root / "force_compact.flag"
    if compact_flag.exists():
        runtime.force_compact = True
        try:
            compact_flag.unlink()
        except OSError:
            pass


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _latest_graph_node(event: Any) -> str | None:
    if isinstance(event, dict) and event:
        return str(next(reversed(event)))
    return None


def _merge_graph_update(state: dict[str, Any], event: Any) -> None:
    if not isinstance(event, dict):
        return
    for update in event.values():
        if not isinstance(update, dict):
            continue
        for key, value in update.items():
            if key == "messages":
                state["messages"] = list(add_messages(state.get("messages", []), value))
            else:
                state[key] = value


def _custom_event_needs_checkpoint(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "tool_result":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("ok") is False or bool(result.get("requires_approval"))


def _final_answer_from_event(event: dict[str, Any]) -> str:
    if event.get("type") != "graph_event":
        return ""
    payload = event.get("event")
    if not isinstance(payload, dict):
        return ""
    update = payload.get("final")
    if not isinstance(update, dict):
        return ""
    return str(update.get("final_answer") or "")
