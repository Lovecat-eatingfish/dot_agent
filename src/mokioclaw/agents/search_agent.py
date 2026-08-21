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

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import (
    Writer,
    dedupe_sources,
    execute_tool_by_name,
    last_ai_content,
    parse_json_content,
    tool_result_event,
)

logger = get_logger(__name__)
from mokioclaw.state.graph import MokioGraphState
from mokioclaw.prompts.builder import get_prompt_builder
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.reliability.cost import record_llm_usage
from mokioclaw.reliability.token_budget import (
    OutputTokenRecovery,
    PromptTooLongRecovery,
    filter_unresolved_tool_uses,
    is_truncated,
    model_with_max_tokens,
)
from mokioclaw.tools.web_search_tool import build_web_search_tool
from mokioclaw.orchestration.agent_loop import _invoke_with_recovery


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
    builder = get_prompt_builder(workspace=workspace, runtime=runtime)
    model = create_model()
    search_tool = build_web_search_tool(workspace=workspace)
    search_agent = model.bind_tools([search_tool])
    messages = [
        SystemMessage(content=builder.build("search_agent")),
        *state.get("messages", []),
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
    tool_ok_count = 0
    tool_fail_count = 0
    # ===== 引擎层恢复状态机（对齐 Claude Code） =====
    output_recovery = OutputTokenRecovery()
    prompt_recovery = PromptTooLongRecovery()
    current_agent = search_agent
    # 用 while 而非 for：escalate 时不消耗迭代配额（对齐 code_agent，#1）
    loops_done = 0
    while loops_done < max_loops:
        loops_done += 1
        response = _invoke_with_recovery(current_agent, messages, prompt_recovery)
        if response is None:
            break
        record_llm_usage(response)  # /cost usage 统计
        produced_messages.append(response)
        messages.append(response)

        # ===== max_output_tokens 截断检测与恢复 =====
        if is_truncated(response):
            recovery = output_recovery.on_truncated()
            if recovery is None:
                break
            if recovery["action"] == "escalate":
                current_agent = model_with_max_tokens(search_agent, output_recovery.max_output_tokens_override)
                messages.pop()
                produced_messages.pop()
                loops_done -= 1
                continue
            if recovery["action"] == "resume":
                # 清洗被截断 AIMessage 可能的 partial tool_calls（#2）
                produced_messages = filter_unresolved_tool_uses(produced_messages)
                _sync_search_messages(messages, produced_messages)
                resume_msg = HumanMessage(content=recovery["message"])
                messages.append(resume_msg)
                produced_messages.append(resume_msg)
                continue

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        # 正常进入工具执行前重置截断恢复计数
        output_recovery.reset_for_new_turn()

        for call in tool_calls:
            args = call.get("args") or {}
            query = str(args.get("query", ""))
            if query:
                queries.append(query)
            writer({"type": "tool_call", "node": "searchAgent", "name": call.get("name"), "args": args})
            tool_result = execute_tool_by_name(
                [search_tool],
                call,
                hook_runner=getattr(runtime, "hook_runner", None),
                budget=getattr(runtime, "result_budget", None),
                workspace=getattr(runtime, "workspace", None),
                runtime=runtime,
            )
            event = tool_result_event(tool_result, node="searchAgent")
            tool_events.append(event)
            writer(event)
            parsed = parse_json_content(tool_result.content)
            if isinstance(parsed, dict):
                if parsed.get("ok") is False:
                    tool_fail_count += 1
                else:
                    tool_ok_count += 1
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
            else:
                tool_fail_count += 1
            produced_messages.append(tool_result)
            messages.append(tool_result)

    summary = last_ai_content(produced_messages) or "\n".join(answers)
    # ===== 悬空 tool_use 清洗：循环跳出时补占位，防 API 400 =====
    produced_messages = filter_unresolved_tool_uses(produced_messages)
    deduped_sources = dedupe_sources(sources)
    ok = True
    error = ""
    if tool_fail_count > 0 and tool_ok_count == 0 and not deduped_sources:
        ok = False
        error = "all search tool calls failed"
    elif not queries and not (summary or "").strip():
        ok = False
        error = "no search performed and empty summary"

    result = {
        "ok": ok,
        "summary": summary,
        "queries": queries,
        "sources": deduped_sources,
        "messages": produced_messages,
        "tool_events": tool_events,
    }
    if error:
        result["error"] = error
    writer(
        {
            "type": "search_summary",
            "summary": result["summary"],
            "queries": result["queries"],
            "sources": result["sources"],
            "ok": ok,
        }
    )
    return result


def _sync_search_messages(messages: list[Any], produced: list[Any]) -> None:
    """把 produced 的清洗结果同步回 messages（resume 路径用，#2）

    不变量：messages = [SystemMessage, HumanMessage] + produced（公共追加部分），
    即 len(messages) == len(produced) + 2；清洗后 produced 多出的尾部即占位消息。
    """
    append_count = len(produced) - (len(messages) - 2)
    if append_count > 0:
        messages.extend(produced[-append_count:])


