import json
import re
from typing import Any

from langgraph.config import get_stream_writer
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from core.state import DotAgentGraphState, TodoItem
from model.openai_provider import create_model
from tools.todo_tool import persist_todos

AMIYA_TODOS = [
    "研究阿米娅并收集可靠来源链接。",
    "创建 amiya_profile.html，写一份精美的角色介绍页面。",
    "在 HTML 中包含至少两个来源链接。",
    "对生成的 HTML 文件运行非交互式检查。",
]

AMIYA_CRITERIA = [
    "amiya_profile.html 存在于工作区中。",
    "页面提到 阿米娅 和 明日方舟。",
    "页面介绍身份、特质、能力和故事角色定位。",
    "页面包含至少两个来源链接。",
]

AMIYA_COMMANDS = [
    "python -c \"from pathlib import Path; p=Path('amiya_profile.html'); s=p.read_text(encoding='utf-8'); assert '阿米娅' in s and '明日方舟' in s; assert s.lower().count('http') >= 2; print('amiya html ok')\"",
]

DEFAULT_TODOS = [
    "明确交付物和验收标准。",
    "委派任务所需的专业工作。",
    "验证生成的结果。",
]

INTENT_ROUTER_PROMPT = """你是 MokioClaw 的意图路由器。

将用户的最新输入精确分类到以下路由之一：

- chat: 问候、感谢、身份/帮助问题、普通的概念性问答，或不需要工作区访问权限的对话消息。
- workflow: 任何需要创建/编辑/读取文件、运行命令、安装包、搜索网络、检查当前项目、验证结果，或生成具体交付物的请求。

当提供会话上下文时，仅用于理解最新输入是否是先前编码工作的延续。像 "继续"、"修一下" 或 "运行测试" 这样的简短跟进，如果涉及之前的工作区工作，则应为 workflow。

只返回以下格式的 JSON：
{"route":"chat"|"workflow","reason":"简要原因","confidence":0.0}

如果不确定，选择 workflow。
"""

CHAT_RESPONDER_PROMPT = """你是 MokioClaw 的轻量级聊天节点。

直接简洁地回答用户。不要声称你读取了文件、搜索了网络、运行了命令、编辑了文件或检查了工作区。
如果用户需要工具或项目上下文的工作，告诉他们应该由 workflow 路由处理。

如果提供了会话上下文，你可以使用最近的对话摘要来回应用户的跟进问题，但不要虚构工作区的事实。
"""


# 意图识别节点
def intent_router_node(state: DotAgentGraphState):
    writer = _get_writer()
    route = "workflow"
    reason = "router fallback: default to workflow"
    confidence = 0.0
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=INTENT_ROUTER_PROMPT),
                HumanMessage(content=build_user_input(state)),
            ]
        )
        parsed = _extract_json(str(response.content)) or {}
        candidate = str(parsed.get("route", "")).strip().lower()
        parsed_confidence = _coerce_confidence(parsed.get("confidence"))
        if candidate in {"chat", "workflow"} and parsed_confidence >= 0.55:
            route = candidate
            confidence = parsed_confidence
            reason = str(parsed.get("reason") or "")
        else:
            reason = str(parsed.get("reason") or "router returned low-confidence or invalid route")
            confidence = parsed_confidence
    except Exception as exc:
        reason = f"router error: {type(exc).__name__}: {exc}"

    event = {
        "type": "intent_decision",
        "route": route,
        "reason": reason,
        "confidence": confidence,
    }
    writer(event)
    return {
        "intent_route": route,
        "intent_reason": reason,
        "intent_confidence": confidence,
    }


# 意图识别 需要去的节点
def intent_route_fn(state: DotAgentGraphState) -> str:
    return "chat_responder" if state.get("intent_route") == "chat" else "planner"


# 聊天节点
def chat_responder_node(state: DotAgentGraphState) -> dict[str, Any]:
    writer = _get_writer()
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=CHAT_RESPONDER_PROMPT),
                HumanMessage(content=build_user_input(state)),
            ]
        )
        text = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        text = f"这是轻量聊天分支，但模型回复暂不可用：{type(exc).__name__}: {exc}"
    if not text:
        text = "我在。你可以继续提问，或者直接描述一个需要我完成的任务。"
    event = {
        "type": "chat_response",
        "mode": "lightweight",
        "reason": state.get("intent_reason", ""),
        "response": text,
    }
    writer(event)
    return {"chat_response": text, "final_answer": text}


# 聊天节点

def planner_node(state: DotAgentGraphState) -> dict[str, Any]:
    writer = _get_writer()
    working_state: DotAgentGraphState = {**state}
    if not working_state.get("todos"):
        _apply_plan(working_state, _default_plan(working_state["task"]))
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    # 构建短期记忆
    memory = build_layered_memory(working_state, node="planner")
    writer(memory_event(memory, node="planner"))
    model = create_model()
    planner = model.bind_tools(_build_planner_tools(working_state, writer))
    messages: list[Any] = [
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=_planner_input(working_state, memory)),
    ]
    produced_messages: list[Any] = []

    writer(
        {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "verification_commands": working_state.get("verification_commands", []),
            "attempts": working_state.get("attempts", 0),
        }
    )

    for _ in range(8):
        response = planner.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = _execute_planner_tool(working_state, writer, call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content="planner stopped after the maximum supervisor tool loop count."))

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = _last_ai_content(produced_messages)
    final_memory = build_layered_memory(working_state, node="planner")
    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "research_notes": working_state.get("research_notes", ""),
        "sources": working_state.get("sources", []),
        "agent_handoffs": working_state.get("agent_handoffs", []),
        "code_agent_summary": working_state.get("code_agent_summary", ""),
        "last_actor_summary": working_state.get("code_agent_summary", ""),
        "messages": produced_messages,
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
    }


# 解析大模型返回的json数据
def _extract_json(text, str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# 解析大模型返回的置信度
def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


# 构建用户上下文数据
def build_user_input(state: DotAgentGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


# get_stream_writer() 是 LangGraph 提供的一个在节点内部获取流式写入器的函数，它的核心作用是让节点在执行过程中能向调用者实时发送自定义数据，从而支持流式输出。
# 获取实时的输出器
# 它之所以需要被 try...except 包裹，是因为它只能在流式执行的上下文中被调用。在非流式执行环境下调用它会抛出 RuntimeError，因此代码中捕获了这个异常，并返回一个空操作作为降级方案。
def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None


# 应用plan
def _apply_plan(state: DotAgentGraphState, plan: dict[str, Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = _todo_items([str(item) for item in plan.get("todos", [])], existing=state.get("todos", []))
    state["acceptance_criteria"] = [str(item) for item in plan.get("acceptance_criteria", [])]
    state["verification_commands"] = _verification_commands_for_task(state["task"], plan)


# 使用默认的plan
def _default_plan(task: str) -> dict[str, Any]:
    return {
        "plan_summary": "协调专业代理，完成并验证所请求的交付物。",
        "todos": [
            "明确交付物和验收标准。",
            "委派任务所需的专业工作。",
            "验证生成的结果。"
        ],
        "acceptance_criteria": [
            "所请求的交付物已存在。",
            "验证模型确认任务已完成。"
        ],
        "verification_commands": [],
    }


# 转化为 TodoItem 对象
def _todo_items(todos: list[str], *, existing: list[dict[str, Any]] | None = None) -> list[TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[TodoItem] = []
    for idx, todo in enumerate(todos, start=1):
        previous = existing_by_content.get(todo, {})
        items.append(
            {
                "id": str(previous.get("id") or f"todo-{idx}"),
                "content": todo,
                "status": str(previous.get("status") or "pending"),
                "note": str(previous.get("note") or ""),
            }
        )
    return items


# 校验任务的验证命令
def _verification_commands_for_task(task: str, parsed: dict[str, Any]) -> list[str]:
    return [str(item) for item in parsed.get("verification_commands") or []]
