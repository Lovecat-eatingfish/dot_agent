"""
代码智能体模块

代码智能体（codeAgent）是 MokioClaw 的核心执行者，负责：
1. 创建和编辑文件
2. 执行 Shell 命令
3. 运行测试和检查
4. 更新待办事项状态

工具集：
- FileReadTool: 读取文件
- FileWriteTool: 写入文件
- FileEditTool: 编辑文件
- GrepTool: 搜索文件内容
- BashTool: 执行 Shell 命令
- NotepadReadTool: 读取笔记本
- NotepadAppendTool: 写入笔记本
- TodoUpdateTool: 更新待办事项状态

执行流程：
1. 接收 planner 的指令
2. 分析任务需求
3. 调用工具执行任务
4. 更新待办事项状态
5. 返回执行摘要
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.core.utils import Writer, execute_tool_calls, last_ai_content, sanitize_user_input, tool_result_event as build_tool_result_event

logger = get_logger(__name__)
from mokioclaw.memory.memory import build_layered_memory, format_layered_memory_for_prompt, memory_event
from mokioclaw.state.graph import MokioGraphState
from mokioclaw.prompts.agent_prompt import CODE_AGENT_PROMPT
from mokioclaw.prompts.builder import get_prompt_builder
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.reliability.token_budget import (
    BudgetTracker,
    OutputTokenRecovery,
    PromptTooLongRecovery,
    filter_unresolved_tool_uses,
    is_truncated,
    model_with_max_tokens,
    NUDGE_MESSAGE,
)
from mokioclaw.tools import build_tools
from mokioclaw.tools.todo_tool import persist_todos, update_todo
from mokioclaw.orchestration.agent_loop import _invoke_with_recovery


def run_code_agent(
    state: MokioGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 10,
) -> dict[str, Any]:
    """执行代码智能体

    Args:
        state: 当前工作流状态
        instruction: planner 委派的指令
        writer: 事件写入器，用于实时输出执行过程, 用于 steam 输出的update 事件
        max_loops: 最大工具调用循环次数

    Returns:
        执行结果字典，包含：
        - ok: 是否成功
        - summary: 执行摘要
        - todos: 更新后的待办事项
    """
    runtime = state["runtime"]
    # 转字典列表
    todos = [dict(todo) for todo in state.get("todos", [])]
    writer = writer or (lambda _: None)
    builder = get_prompt_builder(workspace=runtime.workspace, runtime=runtime)
    # 构建三层记忆： 规则层  工作记忆 历史摘要
    memory = build_layered_memory({**state, "todos": todos}, node="codeAgent")
    writer(memory_event(memory, node="codeAgent"))
    model = create_model()
    # 使用 mutable container 避免 lambda 捕获旧引用
    _todos_ref = {"todos": todos}

    def _bind():
        return model.bind_tools(build_tools(runtime) + [_build_todo_update_tool(_todos_ref)])

    code_agent = _bind()

    writer(
        {
            "type": "plan_snapshot",
            "node": "codeAgent",
            "plan_summary": state.get("plan_summary", ""),
            "todos": todos,
            "verification_commands": state.get("verification_commands", []),
        }
    )

    messages = [
        SystemMessage(content=builder.build("code_agent")),
        HumanMessage(content=_code_agent_input(state, instruction, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    def _exec(call: dict[str, Any]) -> tuple[ToolMessage, list[dict[str, str]]]:
        return execute_code_agent_tool(runtime, _todos_ref["todos"], call)

    stop_continues = 0
    # ===== 引擎层恢复状态机（对齐 Claude Code） =====
    budget = BudgetTracker(budget=getattr(runtime, "token_budget", None))
    output_recovery = OutputTokenRecovery()
    prompt_recovery = PromptTooLongRecovery()
    current_agent = code_agent
    loops_done = 0
    while loops_done < max_loops:
        loops_done += 1
        response = _invoke_with_recovery(current_agent, messages, prompt_recovery)
        if response is None:
            break
        produced_messages.append(response)
        messages.append(response)
        accounted_tokens = budget.account(response)

        # ===== max_output_tokens 截断检测与恢复 =====
        if is_truncated(response):
            recovery = output_recovery.on_truncated()
            if recovery is None:
                break
            if recovery["action"] == "escalate":
                current_agent = model_with_max_tokens(code_agent, output_recovery.max_output_tokens_override)
                messages.pop()
                produced_messages.pop()
                # 回滚本轮已计入的 token，避免污染预算基线
                budget.total_output_tokens -= accounted_tokens
                loops_done -= 1
                continue
            if recovery["action"] == "resume":
                # 被截断的 AIMessage 可能带 partial tool_calls（finish_reason=length
                # 时 provider 仍可能返回带 id 的不完整 tool_calls），需清洗悬空 tool_use，
                # 否则下一轮 messages 末尾 [AIMessage(tool_calls), HumanMessage] 缺 ToolMessage → API 400（#2）
                produced_messages = filter_unresolved_tool_uses(produced_messages)
                # messages 与 produced_messages 同步：清洗可能已补占位 ToolMessage，对齐两者
                _sync_messages_from_produced(messages, produced_messages)
                resume_msg = HumanMessage(content=recovery["message"])
                messages.append(resume_msg)
                produced_messages.append(resume_msg)
                continue

        # ===== 预算检查：达阈值或收益递减 → 停止 =====
        should_stop, reason = budget.check()
        if should_stop:
            logger.info("codeAgent stopping: %s (budget=%s, used=%d)",
                        reason, budget.budget, budget.total_output_tokens)
            produced_messages.append(HumanMessage(content=NUDGE_MESSAGE))
            break

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            # Stop hook：可 preventContinuation 强制继续（对齐 Claude Code）
            from mokioclaw.core.hooks import fire_stop_hook

            stop_result = fire_stop_hook(
                runtime.hook_runner,
                workspace=str(runtime.workspace),
            )
            if stop_result.prevent_continuation and stop_continues < 2:
                stop_continues += 1
                continue_msg = HumanMessage(
                    content=(
                        stop_result.feedback
                        or stop_result.context_injection
                        or "Stop was blocked by a hook. Continue working on the task."
                    )
                )
                messages.append(continue_msg)
                produced_messages.append(continue_msg)
                writer({"type": "stop_blocked", "node": "codeAgent", "reason": continue_msg.content})
                continue
            break

        # 正常进入工具执行前重置截断恢复计数
        output_recovery.reset_for_new_turn()

        # TodoUpdateTool / LoadMcpTool / ToolSearch：串行；Load/Search 优先
        has_todo_update = any(c.get("name") == "TodoUpdateTool" for c in tool_calls)
        has_load = any(c.get("name") in {"LoadMcpTool", "ToolSearchTool"} for c in tool_calls)
        if has_todo_update or has_load or len(tool_calls) == 1:
            ordered = _order_tool_calls_for_execution(tool_calls)
            results_by_key: dict[Any, tuple[ToolMessage, list[dict[str, str]]]] = {}
            for call in ordered:
                results_by_key[_tool_call_key(call)] = _exec(call)
            results = [results_by_key[_tool_call_key(c)] for c in tool_calls]
        else:
            results = execute_tool_calls(tool_calls, _exec, max_workers=4, writer=writer, node="codeAgent")

        need_rebind = False
        for call, (tool_result, new_todos) in zip(tool_calls, results):
            todos = new_todos
            _todos_ref["todos"] = todos
            event = build_tool_result_event(tool_result, node="codeAgent")
            tool_events.append(event)
            writer(event)
            if call.get("name") == "TodoUpdateTool":
                persist_todos(
                    runtime,
                    todos,
                    state.get("acceptance_criteria", []),
                    state.get("verification_commands", []),
                    state.get("plan_summary", ""),
                )
                writer(
                    {
                        "type": "todo_update",
                        "node": "codeAgent",
                        "plan_summary": state.get("plan_summary", ""),
                        "todos": todos,
                        "verification_commands": state.get("verification_commands", []),
                    }
                )
            if call.get("name") in {"LoadMcpTool", "ToolSearchTool"}:
                need_rebind = True
            produced_messages.append(tool_result)
            messages.append(tool_result)

        # 加载延迟工具后重新 bind
        if need_rebind and (
            getattr(runtime, "loaded_mcp_tools", None) or getattr(runtime, "loaded_tools", None)
        ):
            code_agent = _bind()
            # 保持 escalate 后的 max_tokens override（对齐 Claude Code maxOutputTokensOverride）
            if output_recovery.escalated and output_recovery.max_output_tokens_override:
                current_agent = model_with_max_tokens(code_agent, output_recovery.max_output_tokens_override)
            else:
                current_agent = code_agent
    else:
        produced_messages.append(
            AIMessage(content="codeAgent stopped after the maximum tool loop count; verifier will inspect current files.")
        )

    # ===== 悬空 tool_use 清洗：循环跳出时补占位，防 API 400 =====
    produced_messages = filter_unresolved_tool_uses(produced_messages)

    summary = last_ai_content(produced_messages)
    any_failed = any(
        event.get("result", {}).get("ok") is False
        for event in tool_events
        if isinstance(event.get("result"), dict)
    )
    return {
        "ok": not any_failed,
        "summary": summary,
        "todos": todos or state.get("todos", []),
        "messages": produced_messages,
        "tool_events": tool_events,
    }


def _tool_call_key(call: dict[str, Any]) -> Any:
    return call.get("id") or id(call)


def _sync_messages_from_produced(messages: list[Any], produced: list[Any]) -> None:
    """把 produced 的清洗结果同步回 messages（resume 路径用）

    filter_unresolved_tool_uses 可能在 produced 末尾补占位 ToolMessage，
    需让 messages 末尾也带上这些占位，保持两者一致，防下一轮 API 400。
    仅同步末尾新增的 ToolMessage（produced 比 messages 多出的尾部）。
    """
    # 找 messages 在 produced 中的对齐点：messages 末尾应是 produced 的前缀子集
    # 简化处理：produced 比 messages 长出的部分追加到 messages
    if len(produced) > len(messages):
        for extra in produced[len(messages):]:
            messages.append(extra)


def _order_tool_calls_for_execution(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LoadMcpTool / ToolSearchTool 最先执行，其余保持相对顺序"""
    priority = {"LoadMcpTool": 0, "ToolSearchTool": 0}
    return [
        call
        for _, call in sorted(
            enumerate(tool_calls),
            key=lambda item: (priority.get(str(item[1].get("name") or ""), 1), item[0]),
        )
    ]


def execute_code_agent_tool(runtime: RuntimeState, todos: list[dict[str, str]], call: dict[str, Any]):
    from mokioclaw.core.hooks import HookEvent, HookPayload, HookRunner
    from mokioclaw.core.tool_gate import gate_tool_call
    from mokioclaw.memory.microcompact import update_file_state_map

    name = call.get("name", "")
    args = call.get("args") or {}
    tool_call_id = call.get("id") or f"{name}-call"
    mid = runtime.next_message_id()

    def _tm(payload: dict[str, Any]) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            name=name,
            tool_call_id=tool_call_id,
            id=mid,
        )

    # agent_mode 门禁
    if name != "TodoUpdateTool":
        blocked = gate_tool_call(runtime, name, args if isinstance(args, dict) else {})
        if blocked is not None:
            return _tm(blocked), todos

    # PreToolUse Hook（跳过 TodoUpdateTool，它是内部状态管理）
    hook_runner = runtime.hook_runner
    if name != "TodoUpdateTool" and hook_runner and isinstance(hook_runner, HookRunner):
        pre_payload = HookPayload(event=HookEvent.PreToolUse, tool_name=name, tool_args=dict(args))
        pre_result = hook_runner.run(HookEvent.PreToolUse, pre_payload)
        if pre_result.blocked:
            return _tm({"ok": False, "error": pre_result.feedback or "blocked by hook"}), todos
        if pre_result.updated_args is not None:
            args = pre_result.updated_args

    error: Exception | None = None
    if name == "TodoUpdateTool":
        result = update_todo(todos, args.get("todo_id", ""), args.get("status", ""), args.get("note", ""))
        if result.get("ok"):
            todos = result["todos"]
    else:
        tools = {tool.name: tool for tool in build_tools(runtime)}
        tool = (
            tools.get(name)
            or (getattr(runtime, "loaded_tools", {}) or {}).get(name)
            or (getattr(runtime, "loaded_mcp_tools", {}) or {}).get(name)
        )
        if tool is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            try:
                result = tool.invoke(args)
            except Exception as exc:
                # catch-and-return：业务异常不崩 loop
                error = exc
                logger.warning("codeAgent tool %s failed: %s", name, exc, exc_info=True)
                result = {"ok": False, "is_error": True, "error": f"{type(exc).__name__}: {exc}"}

    # PostToolUse / PostToolUseFailure Hook
    if name != "TodoUpdateTool" and hook_runner and isinstance(hook_runner, HookRunner):
        post_event = HookEvent.PostToolUseFailure if error else HookEvent.PostToolUse
        hook_runner.run(post_event, HookPayload(
            event=post_event, tool_name=name, tool_args=args, tool_result=result, error=error,
            workspace=str(runtime.workspace),
        ))

    # L1 Tool-Result Budget：大输出落盘（与 execute_tool_by_name 对齐）
    if name != "TodoUpdateTool" and not error and isinstance(result, dict):
        budget = getattr(runtime, "result_budget", None)
        if budget is not None:
            try:
                result = budget.apply(result, name, runtime.workspace)
            except Exception as exc:
                logger.debug("result budget skipped: %s", exc)
        try:
            from mokioclaw.core.context_modifier import apply_context_modifier

            apply_context_modifier(runtime, result)
        except Exception:
            pass
        try:
            update_file_state_map(
                runtime.file_state_map,
                tool_name=name,
                tool_result=result,
                message_id=mid,
            )
        except Exception:
            pass

    return _tm(result if isinstance(result, dict) else {"ok": True, "result": result}), todos


def _build_todo_update_tool(todos_ref: dict[str, list[dict[str, str]]]) -> StructuredTool:
    """构建 TodoUpdateTool，通过 mutable container 引用避免旧引用问题"""
    return StructuredTool.from_function(
        name="TodoUpdateTool",
        func=lambda todo_id, status, note="": update_todo(todos_ref["todos"], todo_id, status, note),
        description="Update one existing todo status. Args: todo_id, status, optional note.",
    )


def _code_agent_input(state: MokioGraphState, instruction: str, memory: dict[str, Any]) -> str:
    parts = [
        f"Task: {sanitize_user_input(state['task'])}",
        f"Planner instruction:\n{instruction}",
    ]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn coding session:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)
