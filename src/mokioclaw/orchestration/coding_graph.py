"""
Coding Agent LangGraph 骨架

职责：图编排、状态定义、条件路由、Session 内存管理。
节点包含核心业务逻辑：上下文压缩、规划、编码执行、校验、人工介入、终止。
"""
from __future__ import annotations

import json
import uuid
from _pyrepl.commands import interrupt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Dict, Generator, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages
from langchain_core.runnables import RunnableConfig

from mokioclaw.core.events import get_event_bus
from mokioclaw.core.log import get_logger
from mokioclaw.core.hook_loader import load_hooks_into_runner
from mokioclaw.core.hooks import HookRunner
from mokioclaw.core.utils import execute_tool_by_name, last_ai_content
from mokioclaw.orchestration.agent_authorizer import AgentAuthorizer, AutoModeClassifier
from mokioclaw.orchestration.mcp_host import MCPHost
from mokioclaw.orchestration.mcp_manager import MCPManager
from mokioclaw.orchestration.session_persistence import (
    AGENT_SESSIONS_DIR,
    SessionPersistence,
    _deserialize_messages,
    diff_messages,
    persist_turn,
)
from mokioclaw.orchestration.skill_host import SkillHost
from mokioclaw.orchestration.skills_manager import SkillsManager
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.reliability.cost import record_llm_usage
from mokioclaw.reliability.git_utils import git_init
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools import build_tools

logger = get_logger(__name__)

# CodingAgentState 中由 graph 管理的字段（用于 selectively merge final state）
_graph_state_keys = (
    "messages",
    "task",
    "task_plan",
    "replan_count",
    "attempt_count",
    "max_attempt",
    "validate_result",
    "tool_artifacts",
    "need_human_intervene",
    "resume_action",
)


def _get_writer():
    """获取事件写入器（与 nodes.py 中的 _get_writer 行为一致）"""
    try:
        from langgraph.config import get_stream_writer
        langgraph_writer = get_stream_writer()
    except RuntimeError:
        langgraph_writer = None

    bus = get_event_bus()

    def writer(event: dict[str, Any]) -> None:
        if langgraph_writer is not None:
            try:
                langgraph_writer(event)
            except Exception:
                pass
        try:
            bus.emit(event)
        except Exception:
            pass

    return writer


# Constants
# ============================================================

REPLAN_THRESHOLD = 3
MAX_ATTEMPT_DEFAULT = 3


# ============================================================
# RuntimeState 构建辅助
# ============================================================

def _build_runtime_from_state(state: CodingAgentState) -> RuntimeState:
    """从 CodingAgentState 的扁平字段拼出最小 RuntimeState，供 build_tools / run_bash 使用。"""
    from mokioclaw.state.runtime import RuntimeState

    return RuntimeState(
        workspace=state.get("workspace") or Path.cwd(),
        approval_mode=state.get("approval_mode", "inline"),
        allowed_tools=state.get("allowed_tools", []),
        disallowed_tools=state.get("disallowed_tools", []),
        approval_handler=state.get("approval_handler"),
        bash_default_timeout_seconds=state.get("bash_default_timeout", 120),
        bash_max_timeout_seconds=state.get("bash_max_timeout", 600),
        bash_max_output_chars=state.get("bash_max_output_chars", 6000),
        loaded_tools=state.get("loaded_tools", {}),
        file_state_map=state.get("file_state_map", {}),
    )


def _inject_session_context(session: Session) -> None:
    """把 Session 级上下文注入到 current_state，节点直接从 state 读取，不依赖外部框架。

    注入的内容：
      - mcp_catalog / mcp_rules / mcp_meta_tools：MCP 动态提示词 + 渐进披露工具
      - skills_catalog / skills_rules / skills_meta_tool：Skills 动态提示词 + 渐进披露工具
      - hook_runner：PreToolUse 拦截
      - session_id：节点内日志、hook 回调时使用
    """
    st = session.current_state
    if session.mcp_host is not None:
        try:
            st["mcp_catalog"] = session.mcp_host.get_catalog_text()
            st["mcp_rules"] = session.mcp_host.get_system_prompt_rules()
            st["mcp_meta_tools"] = session.mcp_host.get_meta_tools()
        except Exception as exc:
            logger.debug("_inject_session_context: mcp context load failed (%s)", exc)
    if session.skill_host is not None:
        try:
            st["skills_catalog"] = session.skill_host.get_catalog_text()
            st["skills_rules"] = session.skill_host.get_system_prompt_rules()
            st["skills_meta_tool"] = session.skill_host.get_meta_tool()
        except Exception as exc:
            logger.debug("_inject_session_context: skills context load failed (%s)", exc)
    st["hook_runner"] = session.hook_runner
    st["session_id"] = session.session_id


def _inject_runtime_fields(session: Session) -> None:
    """将运行时信息注入 current_state，确保 graph 节点能取到。

    workspace 是用户编码目录，优先用 state 里已有的值，
    其次用 session.workspace（由 _apply_runtime_config 设为 Path.cwd() 或用户指定目录），
    兜底用 Path.cwd()。不要使用 session.persistence.session_dir()（那是 agent 内部存储）。
    """
    st = session.current_state
    if not st.get("workspace"):
        st["workspace"] = session.workspace or Path.cwd()
    if not st.get("approval_mode"):
        st["approval_mode"] = "inline"
    # 其余字段保持 state 中已有值（由上层 create_runtime 或 build_initial_state 写入）


# ============================================================
# Graph State
# ============================================================

class CodingAgentState(TypedDict, total=False):
    """Coding Agent 图的运行时状态（内存为权威源）"""



    # --- RuntimeState 拆出的必需字段（供 build_tools / run_bash 使用）---
    approval_mode: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    approval_handler: Any  # Callable | None
    bash_default_timeout: int
    bash_max_timeout: int
    bash_max_output_chars: int
    loaded_tools: dict[str, Any]
    file_state_map: dict[str, str]

    # --- Session 级上下文（注入到 state，节点直接从 state 读取，不依赖外部框架）---
    mcp_catalog: str       # MCP 工具目录文本（name + description）
    mcp_rules: str         # MCP 调用规则
    mcp_meta_tools: list[dict[str, Any]]  # mcp_search 元工具定义
    skills_catalog: str    # Skills 目录文本
    skills_rules: str      # Skills 使用规则
    skills_meta_tool: dict[str, Any]  # invoke_skill 元工具定义
    hook_runner: Any       # HookRunner — 供 PreToolUse 拦截使用
    session_id: str        # 当前 session ID


# ============================================================
# Nodes（核心业务逻辑）
# ============================================================

def context_compress_node(state: CodingAgentState) -> dict[str, Any]:
    """上下文压缩节点

    检测 messages token 是否超限，超限则调用五层压缩流水线。
    只修改 messages 字段，不碰其他 state 字段。
    仅在入口执行一次，replan 回跳 plan_node 不会再次触发此节点。
    """
    from mokioclaw.compact.compact import apply_compression_pipeline
    from mokioclaw.compact.compact_guard import estimate_token, get_auto_compact_threshold
    from mokioclaw.compact.types import CompactConfig, CompactState

    messages: list[Any] = list(state.get("messages", []))

    config = CompactConfig()
    est = estimate_token(messages)
    threshold = get_auto_compact_threshold(config)

    if est <= threshold:
        return {}

    logger.info("context_compress_node: %d tokens > threshold %d, compressing", est, threshold)

    compact_state = CompactState()
    compressed: list[Any] = apply_compression_pipeline(messages, config, compact_state)

    state.setdefault("messages", compressed)
    return {"messages": compressed}


def plan_node(state: CodingAgentState) -> dict[str, Any]:
    """规划节点

    读取 messages，输出 task_plan。
    可以看到历史 messages，包含上一次 plan 失败的原因。
    """
    writer = _get_writer()
    messages: list[Any] = list(state.get("messages", []))
    existing_plan: dict = state.get("task_plan", {})
    error_feedback = existing_plan.get("error_feedback", "")
    replan_count = int(state.get("replan_count", 0))

    # 构建 system prompt
    replan_hint = ""
    if replan_count > 0:
        replan_hint = (
            f"\n\n[Replan #{replan_count}] Previous plan was rejected. "
            f"Reason: {error_feedback}\n"
            "Generate a NEW plan addressing the issue above."
        )

    # todo： 提示词抽取 + 完善
    system_prompt = (
        "You are a coding task planner. Output a structured plan as JSON.\n"
        "Fields:\n"
        '  description: str — overall plan description\n'
        '  subtasks: list[{id: str, description: str, status: str}] — execution steps\n'
        '  validation_commands: list[str] — bash commands to verify the result\n'
        '  constraints: list[str] — execution constraints\n'
        '  error_feedback: str — leave empty on first plan\n'
        "Respond with ONLY the JSON object, no markdown fences."
    )
    if replan_count > 0:
        system_prompt += replan_hint
    response = None
    try:
        # todo：可以重试三次生成这个 plan计划，每次如果生成错误 把error 信息 再次拼接 给llm 重新生成
        config = RunnableConfig(**{"response_format": {"type": "json_object"}})
        response = create_model().invoke(
            [SystemMessage(content=system_prompt), *messages],
            response_format={"type": "json_object"}
        )
        state['messages'].append(AIMessage(response.content))
        text = str(getattr(response, "content", "") or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        plan: dict[str, Any] = json.loads(text) if text else {}
    except Exception as exc:
        logger.warning("plan_node: LLM plan generation failed: %s", exc, exc_info=True)
        plan = {
            "description": "",
            "subtasks": [],
            "validation_commands": [],
            "constraints": [],
            "error_feedback": "",
        }

    plan.setdefault("description", "")
    plan.setdefault("subtasks", [])
    plan.setdefault("validation_commands", [])
    plan.setdefault("constraints", [])
    plan.setdefault("error_feedback", "")

    state['task_plan'] = plan


    writer({"type": "plan_created", "plan": plan, "replan_count": replan_count})

    return {"task_plan": plan, "messages": [response.content]}


def coding_agent_node(state: CodingAgentState) -> dict[str, Any]:
    """编码执行节点

    根据 task_plan 执行编码相关工作。执行过程中识别 task_plan 是否不合理不可执行。
    """
    writer = _get_writer()
    task_plan: dict = state.get("task_plan", {})
    runtime = _build_runtime_from_state(state)

    # Build tools from runtime
    tools: list[Any] = []
    try:
        tools = build_tools(runtime) if runtime else []
    except Exception as exc:
        logger.warning("coding_agent_node: tool build failed: %s", exc, exc_info=True)

    plan_description = task_plan.get("description", "")
    subtasks = task_plan.get("subtasks", [])
    constraints = task_plan.get("constraints", [])

    # --- 静态提示词 ---
    system_prompt = (
        "You are a coding agent. Execute the given plan step by step.\n"
        f"Plan: {plan_description}\n"
        f"Subtasks: {json.dumps(subtasks, ensure_ascii=False)}\n"
        f"Constraints: {json.dumps(constraints, ensure_ascii=False)}\n"
        "Use the available tools to complete the tasks.\n"
        "After completing, evaluate if the plan was reasonable and executable."
    )

    # --- 从 state 读取 session 上下文（已由 _inject_session_context 注入）---
    # 静态文件：用户配置（自定义指令 + .mokioclaw 规则）
    static_context = state.get("custom_instructions", "")
    if static_context:
        system_prompt += f"\n\n[Static Context]\n{static_context}"

    # 动态提示词：MCP 工具目录 + 规则
    mcp_catalog = state.get("mcp_catalog", "")
    mcp_rules = state.get("mcp_rules", "")
    if mcp_catalog:
        system_prompt += f"\n\n[Available MCP Tools]\n{mcp_catalog}"
    if mcp_rules:
        system_prompt += f"\n{mcp_rules}"
    # 追加 MCP 元工具到 tools
    meta_mcp_tools = state.get("mcp_meta_tools", [])
    if meta_mcp_tools:
        tools.extend(meta_mcp_tools)

    # 动态提示词：Skills 目录 + 规则
    skills_catalog = state.get("skills_catalog", "")
    skills_rules = state.get("skills_rules", "")
    if skills_catalog:
        system_prompt += f"\n\n[Available Skills]\n{skills_catalog}"
    if skills_rules:
        system_prompt += f"\n{skills_rules}"
    # 追加 Skills 元工具到 tools
    meta_skill_tool = state.get("skills_meta_tool", {})
    if meta_skill_tool:
        tools.append(meta_skill_tool)

    # Hook runner（从 state 读取）
    hook_runner = state.get("hook_runner")

    messages: list[Any] = list(state.get("messages", []))
    agent_messages: list[Any] = [SystemMessage(content=system_prompt), *messages]
    # 记录本轮新增的消息（LLM 响应 + tool 结果），全部落盘到 state
    # 记录本轮新增的消息（LLM 响应 + tool 结果），全部落盘到 state
    new_messages: list[Any] = []

    # Execute tool-calling loop
    max_loops = 10
    loop_count = 0
    tool_call_count = 0

    while loop_count < max_loops:
        loop_count += 1
        try:
            response = create_model().bind_tools(tools).invoke(agent_messages)
        except Exception as exc:
            logger.warning("coding_agent_node: model invoke failed: %s", exc, exc_info=True)
            break

        record_llm_usage(response)
        agent_messages.append(response)
        new_messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        tool_call_count += len(tool_calls)
        writer({"type": "tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_name = call.get("name", "")

            # --- Hook 拦截检查 ---
            if hook_runner is not None:
                hook_result = _check_hook(
                    hook_runner,
                    state.get("session_id", ""),
                    state.get("workspace"),
                    tool_name,
                    call.get("args") or {},
                )
                if hook_result.blocked:
                    err_msg = ToolMessage(
                        content=hook_result.feedback or f"Blocked by hook: {tool_name}",
                        tool_call_id=call.get("id", f"call-{tool_call_count}"),
                    )
                    agent_messages.append(err_msg)
                    new_messages.append(err_msg)
                    writer({"type": "tool_blocked", "name": tool_name, "reason": hook_result.feedback})
                    continue

            try:
                tool_msg = execute_tool_by_name(
                    tools=tools,
                    call=call,
                    hook_runner=hook_runner,
                    budget=getattr(runtime, "result_budget", None),
                    workspace=state.get("workspace"),
                    runtime=runtime,
                )
                agent_messages.append(tool_msg)
                new_messages.append(tool_msg)

            except Exception as exc:
                logger.warning("coding_agent_node: tool execution failed: %s", exc, exc_info=True)
                err_msg = ToolMessage(
                    content=f"Error: {exc}",
                    tool_call_id=call.get("id", f"call-{tool_call_count}"),
                )
                agent_messages.append(err_msg)
                new_messages.append(err_msg)

    summary = last_ai_content(agent_messages) or f"Executed {tool_call_count} tool calls in {loop_count} loops"

    # Evaluate plan quality
    plan_reasonable, plan_issue = _evaluate_plan_quality(
        plan_description=plan_description,
        subtasks=subtasks,
        tool_call_count=tool_call_count,
        loop_count=loop_count,
        summary=summary,
    )

    replan_count = int(state.get("replan_count", 0))

    if not plan_reasonable:
        issue_msg = AIMessage(content=f"[Plan Issue] {plan_issue}")
        state.get('messages').append(issue_msg)
        if replan_count < REPLAN_THRESHOLD:
            new_replan = replan_count + 1
            updated_plan = dict(task_plan)
            updated_plan["error_feedback"] = plan_issue
            return {
                "task_plan": updated_plan,
                "replan_count": new_replan,
                "need_human_intervene": False,
            }
        else:
            return {
                "need_human_intervene": True,
                "replan_count": replan_count,
            }

    state.setdefault('messages', agent_messages)
    return {
        "need_human_intervene": False,
    }


# todo： llm 去校验执行呀， 自己执行校验的命令干啥呀
def valid_node(state: CodingAgentState) -> dict[str, Any]:
    """校验节点

    让 agent 自主验证编码结果是否符合预期。
    plan 中的 validation_commands 只作为验证思路参考，不直接执行。
    agent 通过工具（文件读取、bash 等）检查工作区，最终由 agent 判断 passed。
    """
    writer = _get_writer()
    task_plan: dict = state.get("task_plan", {})
    validation_commands: list[str] = task_plan.get("validation_commands", [])
    runtime = _build_runtime_from_state(state)

    # Build tools（与 coding_agent_node 相同的工具集）
    tools: list[Any] = []
    try:
        tools = build_tools(runtime) if runtime else []
    except Exception as exc:
        logger.warning("valid_node: tool build failed: %s", exc, exc_info=True)

    # 构建校验提示词：让 agent 根据 plan 的验证思路自主检查
    plan_description = task_plan.get("description", "")
    subtasks = task_plan.get("subtasks", [])
    system_prompt = (
        "You are a verification agent. Inspect the workspace and determine if the coding task is complete.\n"
        f"Task: {plan_description}\n"
        f"Subtasks: {json.dumps(subtasks, ensure_ascii=False)}\n"
    )
    if validation_commands:
        system_prompt += (
            "\nVerification hints from the plan (use these as guidance, "
            "adapt as needed based on actual workspace state):\n"
        )
        for cmd in validation_commands:
            system_prompt += f"- {cmd}\n"
    system_prompt += (
        "\nUse the available tools to check the workspace. "
        "Return ONLY a JSON object with these fields:\n"
        '  "passed": boolean — true if the task is genuinely complete\n'
        '  "reason": string — brief explanation\n'
        '  "checks": list[{"name": string, "passed": boolean, "detail": string}] — individual checks performed\n'
        "Do NOT include markdown fences or extra text."
    )

    messages: list[Any] = list(state.get("messages", []))
    agent_messages: list[Any] = [SystemMessage(content=system_prompt), *messages]
    new_messages: list[Any] = []

    # Agent 自主校验循环（最多 8 轮工具调用）
    max_loops = 8
    loop_count = 0

    while loop_count < max_loops:
        loop_count += 1
        try:
            response = create_model().bind_tools(tools).invoke(agent_messages)
        except Exception as exc:
            logger.warning("valid_node: model invoke failed: %s", exc, exc_info=True)
            validate_result = {
                "passed": False,
                "error_msg": f"Model invoke failed: {exc}",
                "fail_reason": f"Model invoke failed: {exc}",
                "checks": [],
            }
            writer({"type": "validation_result", "passed": False, "error_msg": str(exc)})
            return {
                "validate_result": validate_result,
                "attempt_count": int(state.get("attempt_count", 0)) + 1,
            }

        record_llm_usage(response)
        agent_messages.append(response)
        new_messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        writer({"type": "validation_tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_name = call.get("name", "")

            # Hook 拦截检查
            hook_runner = state.get("hook_runner")
            if hook_runner is not None:
                hook_result = _check_hook(
                    hook_runner,
                    state.get("session_id", ""),
                    state.get("workspace"),
                    tool_name,
                    call.get("args") or {},
                )
                if hook_result.blocked:
                    err_msg = ToolMessage(
                        content=hook_result.feedback or f"Blocked by hook: {tool_name}",
                        tool_call_id=call.get("id", f"val-{loop_count}"),
                    )
                    agent_messages.append(err_msg)
                    new_messages.append(err_msg)
                    continue

            try:
                tool_msg = execute_tool_by_name(
                    tools=tools,
                    call=call,
                    hook_runner=hook_runner,
                    budget=getattr(runtime, "result_budget", None),
                    workspace=state.get("workspace"),
                    runtime=runtime,
                )
                agent_messages.append(tool_msg)
                new_messages.append(tool_msg)
            except Exception as exc:
                logger.warning("valid_node: tool execution failed: %s", exc, exc_info=True)
                err_msg = ToolMessage(
                    content=f"Error: {exc}",
                    tool_call_id=call.get("id", f"val-{loop_count}"),
                )
                agent_messages.append(err_msg)
                new_messages.append(err_msg)

    # 解析 agent 最终返回的 JSON 校验结果
    validation_result = _parse_validation_result(agent_messages)
    attempt_count = int(state.get("attempt_count", 0)) + 1

    writer({"type": "validation_result", "passed": validation_result["passed"], "error_msg": validation_result.get("error_msg", "")})

    return {
        "validate_result": validation_result,
        "attempt_count": attempt_count,
        "messages": new_messages,
    }


def _parse_validation_result(messages: list[Any]) -> dict[str, Any]:
    """从 agent 消息历史中提取最终的校验 JSON 结果"""
    # 从最后一条 AIMessage 中提取 JSON
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = str(getattr(msg, "content", "") or "").strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            parsed = _extract_json(text)
            if parsed and isinstance(parsed, dict):
                return {
                    "passed": bool(parsed.get("passed", False)),
                    "error_msg": str(parsed.get("reason", "")),
                    "fail_reason": str(parsed.get("reason", "")),
                    "checks": parsed.get("checks", []),
                }
    # 兜底：agent 没返回有效 JSON，标记为失败
    return {
        "passed": False,
        "error_msg": "Verifier did not return valid JSON.",
        "fail_reason": "Verifier did not return valid JSON.",
        "checks": [],
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从文本中提取 JSON 对象"""
    import re
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(raw, start)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# todo： 不行就先不要了
def human_intervene_marker_node(state: CodingAgentState) -> dict[str, Any]:
    """人工介入标记节点

    调用 interrupt() 暂停图执行，等待外部代码传入 resume 值。
    外部 resume 时读取 state["resume_action"]：
      - "continue" → 路由到 plan_node（重置 replan_count）
      - "stop" / 无值 → 路由到 finally_node
    """
    writer = _get_writer()
    need_intervene = state.get("need_human_intervene", False)
    replan_count = state.get("replan_count", 0)
    attempt_count = state.get("attempt_count", 0)

    interrupt_value = {
        "reason": "replan_or_attempt_exhausted",
        "replan_count": replan_count,
        "attempt_count": attempt_count,
        "need_human_intervene": need_intervene,
    }
    writer({"type": "human_intervene", "value": interrupt_value})

    # First call raises Interrupt; resume returns the resume value
    resume_data = interrupt(interrupt_value)

    resume_action = "stop"
    if isinstance(resume_data, dict):
        action = resume_data.get("resume_action", "stop")
        if action in ("continue", "stop"):
            resume_action = action

    if resume_action == "continue":
        return {
            "need_human_intervene": False,
            "replan_count": 0,
            "attempt_count": 0,
            "resume_action": "continue",
        }

    return {
        "need_human_intervene": False,
        "resume_action": "stop",
    }


# todo 持久化  session.json  git commit 持久话turn_xxx.json文件
def finally_node(state: CodingAgentState) -> dict[str, Any]:
    """终止节点

    统一收尾，无业务输出。
    图运行到此结束，所有持久化/git commit/turn 快照全部交给外部业务代码处理。
    """
    writer = _get_writer()
    validate_result: dict = state.get("validate_result", {})
    passed = validate_result.get("passed", False)
    task_plan: dict = state.get("task_plan", {})

    summary = task_plan.get("description", "")
    final_answer = f"Task completed. Passed: {passed}. Plan: {summary}"

    writer({"type": "final", "passed": passed, "summary": summary})

    return {"final_answer": final_answer}


# ============================================================
# Helpers
# ============================================================

def _evaluate_plan_quality(
        *,
        plan_description: str,
        subtasks: list[dict],
        tool_call_count: int,
        loop_count: int,
        summary: str,
) -> tuple[bool, str]:
    """评估 plan 是否合理可执行"""
    if not plan_description and not subtasks:
        return False, "Plan is empty — no description or subtasks provided."
    if not subtasks:
        return False, "Plan has no subtasks — nothing to execute."
    if loop_count >= 10 and tool_call_count == 0:
        return False, "Agent exhausted all loops without making any tool calls."
    if not summary or summary == "(no result)":
        return False, "Agent produced no output after execution."
    return True, ""


def _check_hook(
    hook_runner: HookRunner,
    session_id: str,
    workspace: Optional[Path],
    tool_name: str,
    tool_args: dict[str, Any],
) -> Any:
    """执行 PreToolUse Hook，返回合并结果"""
    try:
        from mokioclaw.core.hooks import HookEvent, HookPayload
        result = hook_runner.run(
            HookEvent.PreToolUse,
            HookPayload(
                event=HookEvent.PreToolUse,
                tool_name=tool_name,
                tool_args=tool_args,
                session_id=session_id,
                workspace=str(workspace) if workspace else "",
            ),
        )
        return result
    except Exception as exc:
        logger.debug("coding_agent_node: hook check skipped (%s)", exc)
        # 返回默认允许
        from mokioclaw.core.hooks import HookResult
        return HookResult()


# ============================================================
# Routers
# ============================================================

def route_coding_agent(state: CodingAgentState) -> str:
    """coding_agent_node 之后的条件路由

    - tool_artifacts.plan_invalid=True & replan_count < replan_max → "plan_node"
    - tool_artifacts.plan_invalid=True & replan_count >= replan_max → "human_intervene_marker"
    - 其他 → "valid_node"
    """
    tool_artifacts: dict = state.get("tool_artifacts", {})
    if tool_artifacts.get("plan_invalid"):
        replan_count = int(state.get("replan_count", 0))
        if replan_count < REPLAN_THRESHOLD:
            return "plan_node"
        return "human_intervene_marker"
    return "valid_node"


def route_valid_node(state: CodingAgentState) -> str:
    """valid_node 之后的条件路由

    - passed == true → "finally_node"
    - passed == false & attempt_count < max_attempt → "coding_agent_node"
    - else → "human_intervene_marker"
    """
    validate_result: dict = state.get("validate_result", {})
    if validate_result.get("passed"):
        return "finally_node"

    attempt_count = int(state.get("attempt_count", 0))
    max_attempt = int(state.get("max_attempt", MAX_ATTEMPT_DEFAULT))

    if attempt_count < max_attempt:
        return "coding_agent_node"
    return "human_intervene_marker"


def route_human_intervene(state: CodingAgentState) -> str:
    """human_intervene_marker 之后的条件路由

    - resume_action == "continue" → "plan_node"
    - 其他（stop / 无值）→ "finally_node"
    """
    resume_action = state.get("resume_action", "stop")
    return "plan_node" if resume_action == "continue" else "finally_node"


# ============================================================
# Graph Builder（每个 session 只 compile 一次）
# ============================================================

def build_graph() -> StateGraph:
    """构建并返回未编译的图。外部调用 .compile() 拿到 CompiledGraph。

    图结构：
        START → context_compress → plan_node → coding_agent_node
                                                       │
                                           ┌───────────┼───────────────────┐
                                           │           │                   │
                                     plan_invalid   plan_valid          plan_invalid
                                     replan<3         │                replan>=3
                                           │           │                   │
                                           ▼           ▼                   ▼
                                       plan_node   valid_node     human_intervene_marker
                                                                           │
                                                                           ▼
                                                                       finally_node → END
    """
    graph = StateGraph(CodingAgentState)

    graph.add_node("context_compress", context_compress_node)
    graph.add_node("plan_node", plan_node)
    graph.add_node("coding_agent_node", coding_agent_node)
    graph.add_node("valid_node", valid_node)
    graph.add_node("human_intervene_marker", human_intervene_marker_node)
    graph.add_node("finally_node", finally_node)

    # 固定链路
    graph.add_edge(START, "context_compress")
    graph.add_edge("plan_node", "coding_agent_node")
    graph.add_edge("human_intervene_marker", "finally_node")
    graph.add_edge("finally_node", END)

    # 条件路由
    graph.add_conditional_edges(
        "context_compress",
        lambda _state: "plan_node",
        {"plan_node": "plan_node"},
    )

    graph.add_conditional_edges(
        "coding_agent_node",
        route_coding_agent,
        {
            "plan_node": "plan_node",
            "valid_node": "valid_node",
            "human_intervene_marker": "human_intervene_marker",
        },
    )

    graph.add_conditional_edges(
        "valid_node",
        route_valid_node,
        {
            "finally_node": "finally_node",
            "coding_agent_node": "coding_agent_node",
            "human_intervene_marker": "human_intervene_marker",
        },
    )

    graph.add_conditional_edges(
        "human_intervene_marker",
        route_human_intervene,
        {
            "plan_node": "plan_node",
            "finally_node": "finally_node",
        },
    )

    return graph


# ============================================================
# Session 数据结构
# ============================================================

@dataclass
class Session:
    """单个会话的内存数据"""

    session_id: str
    compiled_graph: Any  # CompiledGraph — 图只编译一次

    # LLM 工作上下文
    messages: list[BaseMessage]
    # 当前轮次的原始用户需求
    task: str
    # plan_node 输出
    task_plan: dict
    # 重规划计数（coding 触发 replan 时 +1）
    replan_count: int
    # 编码重试计数（仅编码失败累加，replan 不消耗）
    attempt_count: int
    # valid_node 输出
    validate_result: dict

    # 是否需要人工介入
    need_human_intervene: bool

    workspace: Path # 整个项目得工作目录
    is_running: bool = False  # 是不是在运行
    replan_max: int = REPLAN_THRESHOLD  # plan agent最大交互次数
    max_attempt: int = MAX_ATTEMPT_DEFAULT  # 最大 校验和code agent交互次数
    persistence: Optional[SessionPersistence] = None  # 会话和state持久化
    sessionPath: Optional[Path] = None  # 这个轮会话保存得位置
    current_turn_id: int = 0  # 当前会话的轮数
    mcp_host: Optional[Any] = None  # MCPHost（渐进披露，有状态）
    skill_host: Optional[Any] = None  # SkillHost（渐进披露，有状态）
    authorizer: Optional[AgentAuthorizer] = None  # 审批引擎
    hook_runner: Optional[HookRunner] = None  # Hook 执行引擎


# ============================================================
# SessionManager
# ============================================================

class SessionManager:
    """Session 内存管理器

    管理 session 的内存状态和持久化。
    内部集成了 SessionPersistence 处理磁盘 / git 操作。
    持久化策略：每个 session 对应一个 workspace 目录，graph 只在外部层（stream 结束后）落盘。
    """

    def __init__(self, sessions_root: Optional[Path] = None) -> None:
        self._sessions_root = sessions_root or Path(AGENT_SESSIONS_DIR)
        self._sessions: Dict[str, Session] = {}
        self._warmup()

    def _warmup(self) -> None:
        """扫描磁盘，将已存在的 session 加载到内存"""
        if not self._sessions_root.exists():
            return
        for entry in self._sessions_root.iterdir():
            if not entry.is_dir():
                continue
            sid = entry.name
            if sid in self._sessions:
                continue
            try:
                self._init_session(sid)
                logger.info("session warmup loaded", extra={"session_id": sid})
            except Exception as exc:
                logger.warning("session warmup skipped", extra={"session_id": sid, "error": str(exc)})

    def _init_session(self, session_id: str) -> Session:
        """共用的 session 初始化逻辑（graph / persistence / mcp / skills / authorizer）

        注意两个 workspace 的区别：
          - session_path: agent 内部存储目录（session.json、turns/），在用户编码空间下的 .agent_sessions/ 里
          - user_workspace: 用户编码目录（如 D://test_springboot），由 _apply_runtime_config 注入
                          初始化时先设一个默认值，后面会被 _apply_runtime_config 修正
        """
        compiled_graph = build_graph().compile()

        # 先创建 persistence，sessions_root 后续会随 workspace 更新而更新
        persistence = SessionPersistence(sessions_root=self._sessions_root)
        session_path = persistence.session_dir(session_id)

        # 从磁盘恢复已有状态
        meta = persistence.load_session_meta(session_id)
        raw_disk_messages = meta.get("messages", [])
        disk_messages = _deserialize_messages(raw_disk_messages) if raw_disk_messages else []
        initial_state = build_initial_state(
            messages=disk_messages,
            # user_workspace 不在这里设置，等 _apply_runtime_config 注入
            approval_mode="inline",
        )
        initial_state["replan_count"] = meta.get("replan_count", 0)
        initial_state["attempt_count"] = meta.get("attempt_count", 0)

        # 默认 user_workspace：优先 session_path（warmup 场景），否则 cwd
        # 后续会被 _apply_runtime_config 覆盖为用户的实际编码目录
        user_workspace = session_path if session_path.exists() else Path.cwd()

        # 同步 persistence 的存储根目录到 user_workspace/.agent_sessions/
        persistence.update_workspace(user_workspace)

        mcp_mgr = MCPManager(workspace=user_workspace)
        mcp_host = MCPHost(mcp_mgr)
        skills_mgr = SkillsManager(workspace=user_workspace)
        skill_host = SkillHost(skills_mgr)

        try:
            classifier_model = create_model()
        except Exception:
            classifier_model = None
        classifier = AutoModeClassifier(model=classifier_model)
        authorizer = AgentAuthorizer(classifier=classifier)

        # HookRunner：加载 user_workspace 下的 hook 配置
        hook_runner = HookRunner()
        if user_workspace.exists():
            try:
                load_hooks_into_runner(hook_runner, user_workspace)
            except Exception as exc:
                logger.debug("_init_session: hook load skipped for %s (%s)", session_id, exc)

        session = Session(
            session_id=session_id,
            compiled_graph=compiled_graph,
            current_state=initial_state,
            persistence=persistence,
            workspace=user_workspace,
            mcp_host=mcp_host,
            skill_host=skill_host,
            authorizer=authorizer,
            hook_runner=hook_runner,
            current_turn_id=meta.get("current_turn_id", 0),
        )
        self._sessions[session_id] = session
        return session

    def create_session(self, session_id: Optional[str] = None) -> Session:
        """创建新 session

        - 编译图一次
        - 初始化 state
        - 若磁盘已存在（warmup 已加载），直接复用
        - 否则初始化持久化目录（git init）并存入内存字典
        """
        sid = session_id or str(uuid.uuid4())

        # warmup 已从磁盘加载，直接复用
        if sid in self._sessions:
            return self._sessions[sid]

        session = self._init_session(sid)

        persistence = session.persistence
        persistence.save_session_meta(sid, persistence._empty_session_meta(sid))
        git_init(persistence.session_dir(sid))

        # 目录已创建，补加载 hooks
        if session.workspace and session.workspace.exists() and session.hook_runner is not None:
            try:
                load_hooks_into_runner(session.hook_runner, session.workspace)
            except Exception as exc:
                logger.debug("create_session: hook load skipped (%s)", exc)

        return session

    def get_session(self, session_id: str) -> Session:
        """获取 session，不存在则自动创建"""
        if session_id not in self._sessions:
            return self.create_session(session_id)
        return self._sessions[session_id]

    def destroy_session(self, session_id: str) -> None:
        """清理内存 session"""
        self._sessions.pop(session_id, None)

    def load_session_meta(self, session_id: str) -> dict[str, Any]:
        """读取 session.json"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.load_session_meta(session_id)

    def save_session_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        """写回 session.json"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        session.persistence.save_session_meta(session_id, meta)

    def write_turn_snapshot(
            self,
            session_id: str,
            turn_id: int,
            git_commit_hash: str,
            final_state: dict[str, Any],
            full_messages: list[Any],
    ) -> None:
        """写入 turn 快照"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        session.persistence.write_turn_snapshot(
            session_id, turn_id, git_commit_hash, final_state, full_messages
        )

    def read_turn_snapshot(
            self, session_id: str, turn_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取 turn 快照，返回 (graph_state, snapshot_raw)"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.read_turn_snapshot(session_id, turn_id)

    def list_available_turns(self, session_id: str) -> list[int]:
        """扫描 turns 目录，返回所有合法 turn_id 列表（升序）"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.list_available_turns(session_id)

    def rewind_to_turn(self, session_id: str, target_turn_id: int) -> dict[str, Any]:
        """回滚到指定 turn

        步骤：
        1. 校验 turn 存在，读取快照
        2. git reset --hard
        3. 恢复 session.json["messages"]
        4. 更新 session.json 元信息
        5. 返回 graph_state 供内存恢复
        """
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")

        graph_state = session.persistence.rewind_to_turn(session_id, target_turn_id)

        # 恢复内存 state
        session.current_state = self._restore_state(graph_state, session.current_state)
        session.current_turn_id = target_turn_id

        return graph_state

    def stream_session_events(
            self, session_id: str, new_user_input: Optional[str] = None
    ) -> Generator[dict[str, Any], None, None]:
        """流式驱动图执行，yield 每个事件

        流程：
        1. 校验 is_running 锁
        2. 追加用户输入到 current_state["messages"]
        3. 从持久层加载完整 messages 覆盖（确保内存与磁盘一致）
        4. 调用 compiled_graph.stream 迭代事件 yield
        5. 最终 state 回写 session.current_state
        6. 持久化：diff 新消息 → 追加 session.json → git commit → 写 turn 快照
        7. finally 重置 is_running

        持久化约束：仅在 finally_node 结束后执行，graph 内部禁止磁盘 IO。
        """
        session = self.get_session(session_id)

        if session.is_running:
            raise RuntimeError(f"Session {session_id} is already running")

        # 加载磁盘上的完整消息作为事实源（仅 messages 为空时才加载）
        if session.persistence is not None and not session.current_state.get("messages"):
            disk_meta = session.persistence.load_session_meta(session_id)
            disk_messages = disk_meta.get("messages", [])
            if disk_messages:
                session.current_state["messages"] = disk_messages

        # 追加本轮用户输入
        if new_user_input:
            from langchain_core.messages import HumanMessage
            session.current_state.setdefault("messages", []).append(
                HumanMessage(content=new_user_input)
            )

        # 记录 graph 启动前的完整消息（用于 diff）
        old_full_messages = list(session.current_state.get("messages", []))
        session.is_running = True

        # 注入运行时字段（从 session  workspace 等）
        _inject_runtime_fields(session)
        # 注入 session 上下文（MCP / Skills / Hook），节点从 state 读取
        _inject_session_context(session)

        try:
            config = {"configurable": {"thread_id": session_id}, "recursion_limit": 50}

            # graph 逐步更新，需要收集最后一份完整 state 做回写
            final_state: dict[str, Any] = {}
            for chunk in session.compiled_graph.stream(
                    session.current_state,
                    config=config,
            ):
                # chunk 格式: {node_name: state_update} 或 {node_name: (state_update, metadata)}
                for node_output in chunk.values():
                    if isinstance(node_output, dict):
                        final_state.update(node_output)
                    elif isinstance(node_output, tuple) and node_output:
                        final_state.update(node_output[0])
                yield chunk

            # graph 结束后回写 final state 到内存
            if final_state:
                merged = dict(session.current_state)
                for key in _graph_state_keys:
                    if key in final_state:
                        merged[key] = final_state[key]
                session.current_state = merged

            # graph 结束后，在外部层执行持久化
            if session.persistence is not None and session.workspace is not None:
                final_state = dict(session.current_state)
                turn_id = session.current_turn_id + 1
                session.current_turn_id = turn_id

                persist_turn(
                    persistence=session.persistence,
                    session_id=session_id,
                    turn_id=turn_id,
                    workspace=session.workspace,
                    old_full_messages=old_full_messages,
                    final_state=final_state,
                )

        finally:
            session.is_running = False

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _restore_state(
            self, graph_state: dict[str, Any], current: CodingAgentState
    ) -> CodingAgentState:
        """从快照 graph_state 恢复内存 state，保留类型兼容性"""
        restored = dict(current)
        for key in (
                "messages",
                "task_plan",
                "replan_count",
                "attempt_count",
                "max_attempt",
                "validate_result",
                "tool_artifacts",
                "need_human_intervene",
        ):
            if key in graph_state:
                restored[key] = graph_state[key]
        return restored  # type: ignore[return-value]


# ============================================================
# 全局 SessionManager 单例
# ============================================================

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取全局 SessionManager 单例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


# ============================================================
# State 初始化
# ============================================================

def build_initial_state(
        messages: Optional[list[BaseMessage]] = None,
        replan_max: int = REPLAN_THRESHOLD,
        max_attempt: int = MAX_ATTEMPT_DEFAULT,
        task: str = "",
        *,
        workspace: Any = None,
        approval_mode: str = "inline",
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        bash_default_timeout: int = 120,
        bash_max_timeout: int = 600,
        bash_max_output_chars: int = 6000,
) -> CodingAgentState:
    """构建初始 GraphState

    Args:
        messages: 初始消息列表，None 则空列表
        replan_max: replan 阈值
        max_attempt: 编码重试阈值
        task: 当前轮次原始用户需求
        workspace: 工作区路径
        approval_mode: 审批模式
        allowed_tools: 允许的工具列表
        disallowed_tools: 禁止的工具列表
        bash_default_timeout: bash 默认超时（秒）
        bash_max_timeout: bash 最大超时（秒）
        bash_max_output_chars: bash 最大输出字符数

    Returns:
        完整初始化的 CodingAgentState 字典
    """
    return {
        "messages": list(messages or []),
        "task": task,
        "task_plan": {},
        "replan_count": 0,
        "attempt_count": 0,
        "max_attempt": max_attempt,
        "validate_result": {},
        "tool_artifacts": {},
        "need_human_intervene": False,
        "resume_action": "",
        "workspace": workspace,
        "approval_mode": approval_mode,
        "allowed_tools": list(allowed_tools or []),
        "disallowed_tools": list(disallowed_tools or []),
        "approval_handler": None,
        "bash_default_timeout": bash_default_timeout,
        "bash_max_timeout": bash_max_timeout,
        "bash_max_output_chars": bash_max_output_chars,
        "loaded_tools": {},
        "file_state_map": {},
    }
