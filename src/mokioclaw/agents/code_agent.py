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
from mokioclaw.tools import build_tools
from mokioclaw.tools.todo_tool import persist_todos, update_todo


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
    builder = get_prompt_builder(workspace=runtime.workspace)
    # 构建三层记忆： 规则层  工作记忆 历史摘要
    memory = build_layered_memory({**state, "todos": todos}, node="codeAgent")
    writer(memory_event(memory, node="codeAgent"))
    model = create_model()
    # 使用 mutable container 避免 lambda 捕获旧引用
    _todos_ref = {"todos": todos}
    code_agent = model.bind_tools(build_tools(runtime) + [_build_todo_update_tool(_todos_ref)])

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

    for _ in range(max_loops):
        response = code_agent.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        # TodoUpdateTool 会原地修改 todos 列表，必须串行执行以避免竞态
        has_todo_update = any(c.get("name") == "TodoUpdateTool" for c in tool_calls)
        if has_todo_update or len(tool_calls) == 1:
            results = [_exec(call) for call in tool_calls]
        else:
            results = execute_tool_calls(tool_calls, _exec, max_workers=4, writer=writer, node="codeAgent")

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
            produced_messages.append(tool_result)
            messages.append(tool_result)
    else:
        produced_messages.append(
            AIMessage(content="codeAgent stopped after the maximum tool loop count; verifier will inspect current files.")
        )

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


def execute_code_agent_tool(runtime: RuntimeState, todos: list[dict[str, str]], call: dict[str, Any]):
    name = call.get("name", "")
    args = call.get("args") or {}
    if name == "TodoUpdateTool":
        result = update_todo(todos, args.get("todo_id", ""), args.get("status", ""), args.get("note", ""))
        if result.get("ok"):
            todos = result["todos"]
    else:
        tools = {tool.name: tool for tool in build_tools(runtime)}
        tool = tools.get(name)
        if tool is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            try:
                result = tool.invoke(args)
            except Exception as exc:
                logger.warning("codeAgent tool %s failed: %s", name, exc, exc_info=True)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tool_call_id = call.get("id") or f"{name}-call"
    return ToolMessage(content=json.dumps(result, ensure_ascii=False), name=name, tool_call_id=tool_call_id), todos


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
