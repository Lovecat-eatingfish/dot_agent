"""
搜索智能体模块

搜索智能体（searchAgent）负责资料调研，主要职责：
1. 根据 planner 的指令执行网络搜索
2. 收集可靠的信息来源
3. 整理研究笔记和来源 URL
4. 返回精简的研究摘要

工具集：
- WebSearchTool: 执行网络搜索，返回搜索结果和摘要

执行流程：
1. 接收 planner 的搜索指令
2. 构建搜索查询
3. 执行搜索并收集结果
4. 整理来源和摘要
5. 返回研究结果
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import Writer, dedupe_sources, last_ai_content, parse_json_content, tool_result_event

logger = get_logger(__name__)
from mokioclaw.state.graph import MokioGraphState
from mokioclaw.prompts.agent_prompt import SEARCH_AGENT_PROMPT
from mokioclaw.prompts.builder import get_prompt_builder
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.tools.web_search_tool import build_web_search_tool


def run_search_agent(
    state: MokioGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 4,
) -> dict[str, Any]:
    """执行搜索智能体

    Args:
        state: 当前工作流状态
        instruction: planner 委派的搜索指令
        writer: 事件写入器，用于实时输出执行过程
        max_loops: 最大工具调用循环次数

    Returns:
        搜索结果字典，包含：
        - ok: 是否成功
        - summary: 研究摘要
        - sources: 来源列表
        - queries: 执行的搜索查询
    """
    writer = writer or (lambda _: None)
    runtime = state.get("runtime")
    workspace = getattr(runtime, "workspace", None) if runtime else None
    builder = get_prompt_builder(workspace=workspace)
    model = create_model()
    search_tool = build_web_search_tool()
    search_agent = model.bind_tools([search_tool])
    messages = [
        SystemMessage(content=builder.build("search_agent")),
        HumanMessage(
            content=(
                f"Task: {state['task']}\n\n"
                f"Planner instruction:\n{instruction}\n\n"
                f"Existing research notes:\n{state.get('research_notes', '')}\n\n"
                "Search as needed and finish with a concise research summary plus source URLs."
            )
        ),
    ]

    produced_messages: list[Any] = []
    queries: list[str] = []
    sources: list[dict[str, Any]] = []
    answers: list[str] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(max_loops):
        response = search_agent.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            args = call.get("args") or {}
            query = str(args.get("query", ""))
            if query:
                queries.append(query)
            writer({"type": "tool_call", "node": "searchAgent", "name": call.get("name"), "args": args})
            tool_result = _execute_search_tool(search_tool, call)
            event = tool_result_event(tool_result, node="searchAgent")
            tool_events.append(event)
            writer(event)
            parsed = parse_json_content(tool_result.content)
            if isinstance(parsed, dict):
                if parsed.get("answer"):
                    answers.append(str(parsed["answer"]))
                for item in parsed.get("results", []) or []:
                    if isinstance(item, dict):
                        sources.append(item)
                writer(
                    {
                        "type": "search_results",
                        "query": parsed.get("query", query),
                        "answer": parsed.get("answer", ""),
                        "sources": parsed.get("results", []),
                    }
                )
            produced_messages.append(tool_result)
            messages.append(tool_result)

    summary = last_ai_content(produced_messages) or "\n".join(answers)
    result = {
        "ok": True,
        "summary": summary,
        "queries": queries,
        "sources": dedupe_sources(sources),
        "messages": produced_messages,
        "tool_events": tool_events,
    }
    writer(
        {
            "type": "search_summary",
            "summary": result["summary"],
            "queries": result["queries"],
            "sources": result["sources"],
        }
    )
    return result


def _execute_search_tool(search_tool: Any, call: dict[str, Any]) -> ToolMessage:
    """执行搜索工具调用

    Args:
        search_tool: 搜索工具实例
        call: 工具调用字典

    Returns:
        包含搜索结果的 ToolMessage
    """
    name = call.get("name", "")
    args = call.get("args") or {}
    if name != search_tool.name:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = search_tool.invoke(args)
        except Exception as exc:
            logger.warning("searchAgent tool %s failed: %s", name, exc, exc_info=True)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )
