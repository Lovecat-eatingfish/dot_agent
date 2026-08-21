from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from langgraph.graph import add_messages

from mokioclaw.reliability.checkpoint import CheckpointManager, load_resume_inputs, normalize_checkpoint_mode
from mokioclaw.reliability.session_store import (
    create_session,
    load_session,
    get_latest_session,
    append_user_turn,
    append_assistant_turn,
    save_turn_checkpoint,
    finish_session,
    interrupt_session,
    save_session,
    build_resume_context,
    append_messages_to_session,
    load_session_messages,
    load_turns_up_to,
)
from mokioclaw.core.hook_loader import load_hooks_into_runner
from mokioclaw.core.hooks import HookEvent, HookPayload, HookRunner, fire_session_hook, fire_stop_hook
from mokioclaw.core.log import get_logger
from mokioclaw.core.paths import default_workspace, new_task_workspace
from mokioclaw.core.workspace_detection import resolve_workspace
from mokioclaw.tools.bash_tool import cleanup_background_processes, cleanup_old_outputs
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.reliability.trace import TraceRecorder, normalize_trace_mode
from mokioclaw.orchestration.workflow import build_complex_workflow, build_entry_workflow
from mokioclaw.prompts.builder import reset_prompt_builder
from mokioclaw.prompts.thinking import apply_thinking_mode

logger = get_logger(__name__)

def _load_model_override(workspace: Path) -> str:
    """Load model override set by /model command."""
    try:
        path = workspace / ".mokioclaw" / "model_override"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _apply_model_override(runtime: RuntimeState) -> None:
    """应用 /model 覆盖到 runtime 与 provider（对齐 Claude Code modelSwitch）

    每 turn 的 create_runtime 都会刷新：有覆盖文件 → set_active_model 生效下一轮；
    无覆盖文件 → 清除进程内覆盖，回落 env 默认（/model reset 后恢复）。
    """
    from mokioclaw.providers.openai_provider import set_active_model

    override = _load_model_override(runtime.workspace)
    set_active_model(override or None)
    runtime.model_name = override or os.getenv("MODEL", "")


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
    safe_mode: bool = False,
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

    # auto-memory: 注入学到的偏好到 session context（safe-mode 下跳过）
    auto_memory_ctx = ""
    if not safe_mode:
        try:
            from mokioclaw.memory.auto_memory import auto_memory_summary
            auto_memory_ctx = auto_memory_summary(selected) or ""
        except Exception as exc:
            logger.debug("auto-memory load skipped: %s", exc)

    user_config = None
    if not safe_mode:
        try:
            from mokioclaw.config.loader import load_user_config
            user_config = load_user_config(selected)
        except Exception:
            user_config = None

    # agent_mode：显式参数 > 环境变量 > 用户配置 > auto
    resolved_agent_mode = agent_mode or os.getenv("MOKIO_AGENT_MODE") or ""
    if not resolved_agent_mode:
        resolved_agent_mode = user_config.agent_mode if user_config else "auto"

    runtime = RuntimeState(
        workspace=selected,
        approval_mode=approval_mode,
        agent_mode=resolved_agent_mode,
        allowed_tools=list(user_config.allowed_tools) if user_config else [],
        disallowed_tools=list(user_config.disallowed_tools) if user_config else [],
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int("MOKIO_BASH_DEFAULT_TIMEOUT_SECONDS", _env_int("BASH_DEFAULT_TIMEOUT_MS", 120000) // 1000 if _env_int("BASH_DEFAULT_TIMEOUT_MS", 0) > 0 else 120),
        bash_max_timeout_seconds=_env_int("MOKIO_BASH_MAX_TIMEOUT_SECONDS", _env_int("BASH_MAX_TIMEOUT_MS", 600000) // 1000 if _env_int("BASH_MAX_TIMEOUT_MS", 0) > 0 else 600),
        bash_max_output_chars=_env_int("MOKIO_BASH_MAX_OUTPUT_CHARS", _env_int("BASH_MAX_OUTPUT_LENGTH", 6000)),
        bash_env_file=_env_path("MOKIO_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(checkpoint_mode or os.getenv("MOKIO_CHECKPOINT_MODE", "light")),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(trace_mode or os.getenv("MOKIO_TRACE_MODE", "on")),
    )
    if auto_memory_ctx:
        runtime.session_context_injection = (runtime.session_context_injection or "") + "\n" + auto_memory_ctx

    # /model 运行时覆盖：刷新 provider active model + runtime.model_name
    _apply_model_override(runtime)

    # 加载用户/项目 hooks.json，并触发 SessionStart（safe-mode 下跳过）
    if not safe_mode:
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
        # SessionStart stdout 注入上下文（对齐 Claude Code，追加而非覆盖 auto-memory）
        if result.context_injection:
            runtime.session_context_injection = ((runtime.session_context_injection or "") + "\n" + result.context_injection).strip()

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
            runtime.session_context_injection = ((runtime.session_context_injection or "") + "\n" + result.context_injection).strip()
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
    resume_session_id: str | None = None,
    trace_mode: str | None = None,
    safe_mode: bool = False,
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
                    safe_mode=safe_mode,
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
        safe_mode=safe_mode,
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

    # Session 管理：创建新 session 或恢复已有 session
    workspace_path = state.workspace
    resumed = False
    resume_event: dict[str, Any] | None = None

    if resume_session_id:
        # 明确指定了 session ID：恢复该 session
        session_data = load_session(workspace_path, resume_session_id)
        if session_data:
            resumed = True
    elif resume_path is not None:
        resume_str = str(resume_path)
        if resume_str.startswith("session-"):
            session_data = load_session(workspace_path, resume_str)
            if session_data:
                resumed = True
        else:
            session_data = get_latest_session(workspace_path)
            if session_data:
                resumed = True
    else:
        # 无任何恢复参数：自动恢复最新 session（如果有）
        session_data = get_latest_session(workspace_path)
        if session_data:
            resumed = True

    if resumed and session_data:
        session_id = session_data["session_id"]
        # 加载历史 messages 到 global_messages
        history_messages = load_session_messages(workspace_path, session_id)
        state.global_messages = history_messages
        state.session_id = session_id
        resume_context = build_resume_context(session_data)
        yield {"type": "custom_event", "event": {
            "type": "session_resumed",
            "session_id": session_id,
            "turn_index": session_data.get("turn_index", 0),
            "resume_context": resume_context,
        }}
    else:
        # 新建 session（无历史可恢复，或用户主动 /new）
        session_data = create_session(workspace_path, task or "")
        session_id = session_data["session_id"]
        state.session_id = session_id

    # 添加用户轮次
    current_turn = append_user_turn(workspace_path, session_data, task or "")

    # 保存轮次检查点（用户输入后，执行前）
    save_turn_checkpoint(workspace_path, session_data, current_turn, task or "")

    # 准备工作流输入
    if resumed and session_data.get("turns"):
        # 恢复模式：加载历史消息
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        if resume_event:
            yield {"type": "custom_event", "event": resume_event}
        # 将全局消息注入 workflow 输入，作为对话上下文
        if state.global_messages:
            inputs["messages"] = list(state.global_messages)
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": list(state.global_messages),
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    # 添加 session 信息到输入
    inputs["session_id"] = session_id
    inputs["session_turn"] = current_turn
    if resumed and session_data:
        inputs["session_context"] = build_resume_context(session_data)

    current_state: dict[str, Any] = dict(inputs)
    # 初始化追踪记录器
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)

    # 流式执行复杂业务流程图
    final_answer = ""
    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                yield {"type": "custom_event", "event": event}
            else:
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                yield {"type": "graph_event", "event": event}
                # 提取 final_answer
                final_answer = _final_answer_from_event({"type": "graph_event", "event": event}) or final_answer

    except KeyboardInterrupt:
        cleanup_background_processes()
        fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))
        interrupt_session(workspace_path, session_id)
        trace.end(status="interrupted", latest_node="", final_state=current_state)
        return
    except Exception as exc:
        cleanup_background_processes()
        fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))
        logger.error("workflow failed: %s", exc, exc_info=True)
        interrupt_session(workspace_path, session_id)
        trace.end(status="failed", latest_node="", final_state=current_state)
        return

    cleanup_background_processes()
    # 处理渐进式披露标记：从 AI 回复中提取 [NEED_MCP: xxx] / [NEED_SKILL: xxx]
    if final_answer:
        try:
            from mokioclaw.core.progressive_disclosure import process_markers
            final_answer = process_markers(final_answer, state, workspace_path)
        except Exception as exc:
            logger.debug("progressive disclosure processing skipped: %s", exc)

    # Stop hook
    fire_stop_hook(
        state.hook_runner,
        workspace=str(state.workspace),
        session_id=session_id,
    )
    fire_session_hook(state.hook_runner, HookEvent.SessionEnd, workspace=str(state.workspace))

    # 添加 assistant 轮次并标记 session 完成
    append_assistant_turn(workspace_path, session_data, current_turn, final_answer, state_summary=current_state)

    # 持久化本轮 messages：append 到 session-{id}.json 的 messages[]，同时更新 turn 文件
    turn_messages = current_state.get("messages", [])
    if turn_messages:
        append_messages_to_session(workspace_path, session_data, turn_messages, turn=current_turn)
        # 重新加载最新的 session 数据（append_messages_to_session 已保存）
        session_data = load_session(workspace_path, session_id)
        save_turn_checkpoint(
            workspace_path, session_data, current_turn,
            task or "", state=current_state, turn_messages=turn_messages,
        )

    finish_session(workspace_path, session_id)

    trace_event = trace.end(status="finished", latest_node="", final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}

    # 返回 session 信息
    yield {"type": "custom_event", "event": {
        "type": "session_finished",
        "session_id": session_id,
        "turn": current_turn,
    }}


def stream_session_events(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    resume_session_id: str | None = None,
    trace_mode: str | None = None,
    safe_mode: bool = False,
) -> Iterator[dict[str, Any]]:
    workspace = (session_workspace or default_workspace()).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)

    # Session 管理：优先恢复已有 session，避免每次新建
    resumed = False
    if resume_session_id:
        # 明确指定了 session ID
        session_data = load_session(workspace, resume_session_id)
        if session_data:
            resumed = True
        else:
            session_data = create_session(workspace, task or "")
    elif resume_workspace:
        # workspace 路径：可能是 session-xxx 或普通路径
        resume_str = str(resume_workspace)
        if resume_str.startswith("session-"):
            session_data = load_session(workspace, resume_str)
            if session_data:
                resumed = True
            else:
                session_data = create_session(workspace, task or "")
        else:
            session_data = get_latest_session(workspace)
            if session_data:
                resumed = True
            else:
                session_data = create_session(workspace, task or "")
    else:
        # 无恢复参数：自动恢复最新 session（除非调用方显式传了空 session_id 表示 /new）
        session_data = get_latest_session(workspace)
        if session_data:
            resumed = True
        else:
            session_data = create_session(workspace, task or "")

    session_id = session_data["session_id"]

    yield {"type": "custom_event", "event": {
        "type": "session_started",
        "session_id": session_id,
        "workspace": str(workspace),
        "resumed": resumed,
        "turn_index": session_data.get("turn_index", 0),
    }}
    yield {"type": "workspace", "path": str(workspace)}

    if not task:
        return

    # todo： 思考模式 使用配置配置， 不要猜测用户的意图
    cleaned_task, thinking_instruction = apply_thinking_mode(task)
    task = cleaned_task or task

    # UserPromptSubmit hook
    _prompt_hook_runner = HookRunner()
    try:
        # 给HookRunner注册hook
        load_hooks_into_runner(_prompt_hook_runner, workspace)
    except Exception as exc:
        logger.debug("hook load for UserPromptSubmit skipped: %s", exc)
        # 执行事件是 UserPromptSubmit 用户输入提示词的hook
    _prompt_result = _prompt_hook_runner.run(
        HookEvent.UserPromptSubmit,
        HookPayload(
            event=HookEvent.UserPromptSubmit,
            user_prompt=task,
            workspace=str(workspace),
            session_id=session_id,
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

    # # todo： 显式记忆写入： 待修改： 做梦功能和 feedback功能写入
    # try:
    #     from mokioclaw.memory.auto_memory import maybe_write_explicit_memory, trigger_autodream_if_needed
    #     # todo 待完善
    #     mem_result = maybe_write_explicit_memory(workspace, task)
    #     if mem_result and mem_result.get("ok"):
    #         yield {
    #             "type": "custom_event",
    #             "event": {"type": "memory_write", "path": mem_result.get("path"), "name": mem_result.get("name")},
    #         }
    #     trigger_autodream_if_needed(workspace)
    # except Exception as exc:
    #     logger.debug("auto memory hook skipped: %s", exc)

    # 添加用户轮次并保存检查点
    turn = append_user_turn(workspace, session_data, task)
    save_turn_checkpoint(workspace, session_data, turn, task)

    yield {"type": "custom_event", "event": {
        "type": "session_turn_started",
        "session_id": session_id,
        "turn": turn,
        "task": task[:500],
    }}

    # 意图识别
    route = "workflow"
    entry_state: dict[str, Any] = {
        "task": task or "",
        "messages": session_data['turns'],
        "session_id": session_id,
        "session_turn": turn,
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
        append_assistant_turn(workspace, session_data, turn, response)
        finish_session(workspace, session_id)
        yield {"type": "custom_event", "event": {
            "type": "session_turn_saved",
            "session_id": session_id,
            "turn": turn,
            "route": "chat",
        }}
        return

    # 执行复杂工作流
    workflow_events = _stream_complex_workflow(
        task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
        safe_mode=safe_mode,
        session_data=session_data,
        turn=turn,
        thinking_instruction=thinking_instruction,
        resumed=resumed,
    )
    final_answer = ""
    turn_messages: list[Any] = []
    for event in workflow_events:
        final_answer = _final_answer_from_event(event) or final_answer
        # 捕获本轮 messages 用于持久化
        ev = event.get("event") if isinstance(event, dict) else None
        if isinstance(ev, dict) and ev.get("type") == "turn_messages":
            turn_messages = ev.get("messages", [])
            continue
        yield event

    # 持久化 messages
    if turn_messages:
        append_messages_to_session(workspace, session_data, turn_messages, turn=turn)
        session_data = load_session(workspace, session_id)
        save_turn_checkpoint(
            workspace, session_data, turn,
            task, turn_messages=turn_messages,
        )

    # 添加 assistant 轮次
    append_assistant_turn(workspace, session_data, turn, final_answer)

    yield {"type": "custom_event", "event": {
        "type": "session_turn_saved",
        "session_id": session_id,
        "turn": turn,
        "route": "workflow",
    }}

    # 后台记忆提取
    try:
        from mokioclaw.memory.auto_memory import trigger_background_extraction
        trigger_background_extraction(
            workspace,
            new_messages=[f"user: {task}", f"assistant: {final_answer}"],
            session_id=session_id,
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
    safe_mode: bool = False,
    session_data: dict[str, Any] | None = None,
    turn: int | None = None,
    thinking_instruction: str = "",
    resumed: bool = False,
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
        safe_mode=safe_mode,
    )
    if thinking_instruction:
        state.thinking_instruction = thinking_instruction
    _apply_workspace_runtime_flags(state)

    # 会话级 SessionStart：整段 session 只触发一次
    session_id = session_data.get("session_id", "") if session_data else ""
    if session_data and not session_data.get("_session_hooks_started"):
        fire_session_hook(
            state.hook_runner,
            HookEvent.SessionStart,
            workspace=str(state.workspace),
            session_id=session_id,
        )
        session_data["_session_hooks_started"] = True
        session_data.pop("_session_ended", None)

    # 恢复模式：加载历史消息到 global_messages
    if resumed and session_data:
        history_messages = load_session_messages(workspace, session_id)
        state.global_messages = history_messages
        state.session_id = session_id

    # 每个新任务重置 PromptBuilder，确保使用当前 workspace 的配置
    reset_prompt_builder()
    workflow = build_complex_workflow()

    # 准备 workflow 输入
    inputs: dict[str, Any] = {
        "task": task or "",
        "runtime": state,
        "messages": list(state.global_messages),
        "attempts": 0,
        "max_attempts": max_attempts,
    }

    if session_id:
        inputs["session_id"] = session_id
    if turn is not None:
        inputs["session_turn"] = turn
    if session_data:
        inputs["session_context"] = build_resume_context(session_data)

    current_state: dict[str, Any] = dict(inputs)
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed)

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                yield {"type": "custom_event", "event": event}
            else:
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        cleanup_background_processes()
        if session_data:
            interrupt_session(workspace, session_id)
        trace.end(status="interrupted", final_state=current_state)
        return
    except Exception as exc:
        cleanup_background_processes()
        logger.error("workflow failed: %s", exc, exc_info=True)
        if session_data:
            interrupt_session(workspace, session_id)
        trace.end(status="failed", final_state=current_state)
        return

    cleanup_background_processes()
    fire_stop_hook(
        state.hook_runner,
        workspace=str(state.workspace),
        session_id=session_id,
    )
    trace_event = trace.end(status="finished", final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}

    # 持久化本轮 messages 到 session（内层直接写，不再依赖外层处理 turn_messages 事件）
    from mokioclaw.reliability.checkpoint import serialize_message as _ser_msg
    _msgs = current_state.get("messages", [])
    if _msgs and session_data and session_id:
        try:
            append_messages_to_session(workspace, session_data, _msgs, turn=turn or 0)
            session_data = load_session(workspace, session_id)
            save_turn_checkpoint(
                workspace, session_data, turn or 0,
                task or "", state=current_state, turn_messages=_msgs,
            )
        except Exception as exc:
            logger.debug("session message persist skipped: %s", exc)


def end_persistent_session_hooks(workspace: Path | None) -> None:
    """TUI / 多轮会话结束时触发 SessionEnd（仅当曾发过 SessionStart）

    在 /new、TUI 退出时调用；单次 CLI 流式任务仍由 stream_agent_events 自行收尾。
    """
    if workspace is None:
        return
    workspace = workspace.expanduser()

    session_data = get_latest_session(workspace)
    if not session_data:
        return

    if not session_data.get("_session_hooks_started") or session_data.get("_session_ended"):
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
        session_id=str(session_data.get("session_id", "")),
    )
    session_data["_session_ended"] = True
    session_data["_session_hooks_started"] = False
    save_session(workspace, session_data)


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
