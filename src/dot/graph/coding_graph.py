"""
LangGraph 图编排 — 状态 = Session（单 channel 包装）

设计（对齐 doc/fix.md，自定义机制，不过度依赖 langgraph）：
  - DotAgentState 只有一个 key："session"，值是 Session 对象本身
  - 节点通过 state["session"] 拿到同一个 Session 直接读写（Session IS State）
  - 消息手动 append（session.messages.append），不用 add_messages / RemoveMessage
  - 路由函数从 session 字段读取决策
  - langgraph 只用：StateGraph 节点/边/条件路由 + stream 事件流 +
    get_stream_writer；不用 interrupt / checkpointer / 回滚（全部自定义）
  - 人工介入：human_intervene 节点置 awaiting_intervention 后走 finally
    结束本轮（状态随 turn 持久化）；用户选 continue 由外部重新进图
  - 持久化在 finally 节点内完成：session.json + turn 快照 + agent 专用
    git commit（用户代码回滚）

节点：
  1. context_compress — 上下文压缩（超限裁剪，直接替换 session.messages）
  2. plan_node        — 生成 plan JSON
  3. coding_agent     — 执行编码（工具循环 max 10）
  4. valid_node       — 校验结果（工具循环 max 8）
  5. human_intervene  — 人工介入（置 awaiting 标记 → finally 结束本轮）
  6. finally_node     — 收尾 + 持久化（session.json / turn_xxx.json / git commit）

replan 循环语义：
  - replan_count 每 turn 只在 reset_per_turn 清零，coding_agent 判定 plan
    无效时 +1，达到 replan_max 转人工
  - plan_invalid 是路由标记：coding_agent 置位 → 回 plan_node；plan_node
    生成新 plan 后清除
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..core.hooks import HookEvent, HookPayload, HookResult, HookRunner
from ..core.llm import create_model
from ..core.log import get_logger
from ..tools.meta import build_tools_for_session, dispatch_special_tool
from .prompts import get_coding_system_prompt, get_plan_system_prompt, get_valid_system_prompt
from ..session.session import MAX_ATTEMPT_DEFAULT, REPLAN_THRESHOLD, Session
from ..core.utils import execute_tool_by_name, last_ai_content

logger = get_logger(__name__)

# 工具循环上限
CODING_MAX_LOOPS = 10
VALID_MAX_LOOPS = 8
# 上下文压缩阈值（消息条数）
COMPRESS_MAX_MESSAGES = 100


class DotAgentState(TypedDict, total=False):
    """图状态：单一 channel，值是 Session 对象本身

    final_answer 是 finally_node 的输出 channel（事件流读取，不参与路由）
    """
    session: Session
    final_answer: str


def _get_writer() -> Any:
    """获取 langgraph stream writer；无 stream 上下文时返回 no-op"""
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda event: None


# ============================================================
# Nodes
# ============================================================

def context_compress_node(state: DotAgentState) -> dict[str, Any]:
    """上下文压缩节点

    检测 messages 是否超限，超限则裁剪（保留系统语义 + 最近 N 条），
    直接替换 session.messages。触发前发 PreCompact Hook。
    """
    session = state["session"]
    writer = _get_writer()
    messages = list(session.messages)
    logger.info("[node:context_compress] messages=%d", len(messages))

    if len(messages) <= COMPRESS_MAX_MESSAGES:
        logger.info("[node:context_compress] no compression needed, skip")
        return {}

    # PreCompact Hook（auto 触发）
    if session.hook_runner is not None:
        try:
            session.hook_runner.run(
                HookEvent.PreCompact,
                HookPayload(
                    event=HookEvent.PreCompact,
                    compact_trigger="auto",
                    session_id=session.session_id,
                    workspace=str(session.workspace),
                ),
            )
        except Exception as exc:
            logger.debug("PreCompact hook skipped: %s", exc)

    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
    kept = other_msgs[-COMPRESS_MAX_MESSAGES:]
    compressed = system_msgs + kept
    session.messages = compressed

    logger.info("[node:context_compress] compressed %d -> %d messages", len(messages), len(compressed))
    writer({"type": "context_compressed", "before": len(messages), "after": len(compressed)})
    return {}


def _invoke_llm(messages: list[Any], *, node: str, tools: list[Any] | None = None, config: Any = None) -> Any:
    """统一的 LLM 调用入口（带链路追踪 span）"""
    import os

    from ..trace import get_tracer

    span = get_tracer().start_span(
        "llm", "llm_inference",
        tags={
            "node": node,
            "model": os.environ.get("MODEL", ""),
            "messages": len(messages),
            "with_tools": bool(tools),
        },
    )
    try:
        model = create_model()
        if tools:
            model = model.bind_tools(tools)
        response = model.invoke(messages, config=config) if config is not None else model.invoke(messages)
        content = getattr(response, "content", "")
        tool_calls = getattr(response, "tool_calls", None) or []
        span.set_output_summary(f"content_len={len(str(content))} tool_calls={len(tool_calls)}")
        span.finish()
        return response
    except BaseException as exc:
        span.set_output_summary(f"{type(exc).__name__}: {exc}")
        span.finish(exc)
        raise


def plan_node(state: DotAgentState) -> dict[str, Any]:
    """规划节点：读取 messages，生成 task_plan JSON"""
    session = state["session"]
    writer = _get_writer()
    error_feedback = session.task_plan.get("error_feedback", "")
    logger.info(
        "[node:plan] entering (replan_count=%d, has_feedback=%s)",
        session.replan_count, bool(error_feedback),
    )

    system_prompt = get_plan_system_prompt(
        replan_count=session.replan_count,
        error_feedback=error_feedback,
    )

    plan: dict[str, Any] = {}
    try:
        config = RunnableConfig(**{"response_format": {"type": "json_object"}})
        response = _invoke_llm(
            [SystemMessage(content=system_prompt), *session.messages],
            node="plan_node",
            config=config,
        )
        session.messages.append(response)

        text = _extract_json_text(str(getattr(response, "content", "") or ""))
        plan = json.loads(text) if text else {}
        logger.info("[node:plan] LLM responded, plan keys=%s", list(plan.keys()))
    except Exception as exc:
        logger.warning("plan_node: LLM plan generation failed: %s", exc, exc_info=True)
        plan = {}

    plan.setdefault("description", "")
    plan.setdefault("subtasks", [])
    plan.setdefault("validation_commands", [])
    plan.setdefault("constraints", [])
    plan.setdefault("error_feedback", "")

    session.task_plan = plan
    session.plan_invalid = False  # 新 plan 已生成，清除无效标记
    logger.info("[node:plan] done: desc=%r, subtasks=%d", plan.get("description", ""), len(plan.get("subtasks", [])))
    writer({"type": "plan_created", "plan": plan, "replan_count": session.replan_count})
    return {}


def coding_agent_node(state: DotAgentState) -> dict[str, Any]:
    """编码执行节点：按 task_plan 执行编码，评估 plan 合理性决定路由"""
    session = state["session"]
    writer = _get_writer()
    task_plan = session.task_plan
    logger.info(
        "[node:coding_agent] entering: desc=%r, subtasks=%d, replan=%d/%d",
        task_plan.get("description", ""),
        len(task_plan.get("subtasks", [])),
        session.replan_count, session.replan_max,
    )

    # 构建工具（同时确保 session.runtime 就位）
    try:
        tools = build_tools_for_session(session)
    except Exception as exc:
        logger.warning("coding_agent_node: tool build failed: %s", exc, exc_info=True)
        tools = []

    runtime = session.runtime

    plan_description = task_plan.get("description", "")
    subtasks = task_plan.get("subtasks", [])
    # 约束条件
    constraints = task_plan.get("constraints", [])

    # 静态上下文（G6：用户自定义指令 + 记忆索引），只注入 coding_agent
    static_context = _load_static_context(session.workspace)
    if static_context:
        logger.info("[node:coding_agent] loaded static_context (%d chars)", len(static_context))

    # 动态上下文：MCP / Skills 目录（共享 host 基础目录 + per-session loaded 视图）
    mcp_loaded = set(runtime.loaded_mcp_tools) if runtime is not None else set()
    skill_loaded = set(runtime.loaded_skills) if runtime is not None else set()

    mcp_catalog = _safe_host_call(session.mcp_host, "get_catalog_text", "", mcp_loaded)
    mcp_rules = _safe_host_call(session.mcp_host, "get_system_prompt_rules", "")

    skills_catalog = _safe_host_call(session.skill_host, "get_catalog_text", "", skill_loaded)
    skills_rules = _safe_host_call(session.skill_host, "get_system_prompt_rules", "")
    if mcp_catalog:
        logger.info("[node:coding_agent] mcp_catalog (%d tools)", len(mcp_catalog.split("\n")))
    if skills_catalog:
        logger.info("[node:coding_agent] skills_catalog loaded")

    base_system_prompt = get_coding_system_prompt(
        plan_description=plan_description,
        subtasks=subtasks,
        constraints=constraints,
        static_context=static_context,
        mcp_catalog=mcp_catalog,
        mcp_rules=mcp_rules,
        skills_catalog=skills_catalog,
        skills_rules=skills_rules,
    )

    # Tool-calling 循环
    loop_count = 0
    tool_call_count = 0

    while loop_count < CODING_MAX_LOOPS:
        loop_count += 1

        # Skill 内容可能在本轮中被 invoke_skill 加载 → 每轮重建 system prompt（per-session 累积）
        skill_content = runtime.active_skill_content if runtime is not None else ""
        system_prompt = base_system_prompt + (f"\n\n{skill_content}" if skill_content else "")
        agent_messages: list[Any] = [SystemMessage(content=system_prompt), *session.messages]

        logger.info("[node:coding_agent] loop=%d/%d, messages_in_context=%d", loop_count, CODING_MAX_LOOPS,
                    len(agent_messages))

        try:
            response = _invoke_llm(agent_messages, node="coding_agent", tools=tools)
        except Exception as exc:
            logger.warning("coding_agent_node: model invoke failed: %s", exc, exc_info=True)
            break

        session.messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            logger.info("[node:coding_agent] loop=%d: no tool calls, agent finished", loop_count)
            break

        tool_call_count += len(tool_calls)
        logger.info("[node:coding_agent] loop=%d: %d tool calls", loop_count, len(tool_calls))
        writer({"type": "tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_name = call.get("name", "")
            logger.info("[node:coding_agent] -> tool: %s  args=%s", tool_name, _short_args(call.get("args")))
            tool_msg = _run_tool_call(session, runtime, tools, call, writer, prefix="call")
            if tool_msg is not None:
                session.messages.append(tool_msg)

    summary = last_ai_content(session.messages) or (
        f"Executed {tool_call_count} tool calls in {loop_count} loops"
    )

    # 评估 plan 合理性 → 决定 replan / 人工介入 / 进入校验
    plan_reasonable, plan_issue = _evaluate_plan_quality(
        plan_description=plan_description,
        subtasks=subtasks,
        tool_call_count=tool_call_count,
        loop_count=loop_count,
        summary=summary,
    )

    if not plan_reasonable:
        logger.warning("[node:coding_agent] plan INVALID: %s  (replan=%d/%d)", plan_issue, session.replan_count,
                       session.replan_max)
        session.messages.append(AIMessage(content=f"[Plan Issue] {plan_issue}"))
        if session.replan_count < session.replan_max:
            session.replan_count += 1
            session.task_plan = {**task_plan, "error_feedback": plan_issue}
            session.plan_invalid = True
            session.need_human_intervene = False
            logger.info("[node:coding_agent] -> replan triggered (count=%d)", session.replan_count)
        else:
            session.need_human_intervene = True
            logger.warning("[node:coding_agent] -> need_human_intervene (replan exhausted)")
        writer({"type": "plan_invalid", "issue": plan_issue, "replan_count": session.replan_count})
    else:
        session.need_human_intervene = False
        logger.info("[node:coding_agent] plan OK -> routing to valid_node")

    return {}


def valid_node(state: DotAgentState) -> dict[str, Any]:
    """校验节点：agent 自主验证编码结果"""
    session = state["session"]
    writer = _get_writer()
    task_plan = session.task_plan
    validation_commands = task_plan.get("validation_commands", [])
    logger.info(
        "[node:valid_node] entering: attempt=%d/%d, plan=%r",
        session.attempt_count, session.max_attempt,
        task_plan.get("description", "")[:80],
    )

    try:
        tools = build_tools_for_session(session)
    except Exception as exc:
        logger.warning("valid_node: tool build failed: %s", exc, exc_info=True)
        tools = []
    runtime = session.runtime

    system_prompt = get_valid_system_prompt(
        plan_description=task_plan.get("description", ""),
        subtasks=task_plan.get("subtasks", []),
        validation_commands=validation_commands,
    )

    loop_count = 0
    while loop_count < VALID_MAX_LOOPS:
        loop_count += 1
        agent_messages: list[Any] = [SystemMessage(content=system_prompt), *session.messages]

        try:
            response = _invoke_llm(agent_messages, node="valid_node", tools=tools)
        except Exception as exc:
            logger.warning("valid_node: model invoke failed: %s", exc, exc_info=True)
            session.validate_result = {
                "passed": False,
                "error_msg": f"Model invoke failed: {exc}",
                "fail_reason": f"Model invoke failed: {exc}",
                "checks": [],
            }
            session.attempt_count += 1
            writer({"type": "validation_result", "passed": False, "error_msg": str(exc)})
            return {}

        session.messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        writer({"type": "validation_tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            tool_msg = _run_tool_call(session, runtime, tools, call, writer, prefix="val")
            if tool_msg is not None:
                session.messages.append(tool_msg)

    session.validate_result = _parse_validation_result(session.messages)
    session.attempt_count += 1
    passed = session.validate_result.get("passed", False)
    logger.info(
        "[node:valid_node] result: passed=%s, attempts=%d/%d, reason=%s",
        passed, session.attempt_count, session.max_attempt,
        session.validate_result.get("error_msg", "")[:80],
    )
    writer({"type": "validation_result", "passed": passed})
    return {}


def human_intervene_node(state: DotAgentState) -> dict[str, Any]:
    """人工介入节点（自定义机制，不用 langgraph interrupt）

    置 awaiting_intervention 标记后直接走 finally 结束本轮；
    状态随 turn 持久化，进程重启也能恢复。用户的选择由外部处理：
      - continue → SessionManager.resume_session("continue") 清零计数后
        重新进图（从 plan 重新规划）
      - stop     → 清标记结束
    """
    session = state["session"]
    writer = _get_writer()
    logger.info(
        "[node:human_intervene] entering: replan=%d, attempt=%d",
        session.replan_count, session.attempt_count,
    )

    session.awaiting_intervention = True
    session.need_human_intervene = False
    session.resume_action = "pending"

    writer({
        "type": "human_intervene",
        "value": {
            "reason": "replan_or_attempt_exhausted",
            "replan_count": session.replan_count,
            "attempt_count": session.attempt_count,
        },
    })
    return {}


def finally_node(state: DotAgentState) -> dict[str, Any]:
    """终止节点：收尾 + 持久化（对齐 fix.md，持久化在节点内完成）

    1. turn 计数 +1
    2. session.json 全量覆盖
    3. agent 专用 git commit 用户项目目录（hash 存入 turn 快照）
    4. turns/turn_xxxx.json 快照（rewind 用）
    """
    session = state["session"]
    writer = _get_writer()

    passed = session.validate_result.get("passed", False)
    summary = session.task_plan.get("description", "") or session.task

    final_answer = _last_final_answer(session)

    turn_id = session.current_turn_id + 1
    session.current_turn_id = turn_id
    if session.persistence is not None:
        try:
            from ..session.persistence import persist_turn

            persist_turn(
                persistence=session.persistence,
                session_id=session.session_id,
                turn_id=turn_id,
                session=session,
            )
        except Exception as exc:
            logger.warning("[node:finally] persist_turn failed: %s", exc, exc_info=True)

    logger.info("[node:finally] passed=%s, turn=%d, summary=%r", passed, turn_id, summary[:80])
    writer({"type": "final", "passed": passed, "summary": summary, "answer": final_answer})
    return {"final_answer": final_answer}


# ============================================================
# Routers（从 session 读决策）
# ============================================================

def route_coding_agent(state: DotAgentState) -> str:
    """coding_agent 之后：
    - need_human_intervene → human_intervene
    - plan_invalid（且未达阈值）→ plan_node（replan）
    - 其他 → valid_node
    """
    session = state["session"]
    if session.need_human_intervene:
        return "human_intervene"
    if session.plan_invalid:
        return "plan_node"
    return "valid_node"


def route_valid_node(state: DotAgentState) -> str:
    """valid_node 之后：
    - passed → finally_node
    - failed 且 attempt < max → coding_agent（重试）
    - failed 且 attempt >= max → human_intervene
    """
    session = state["session"]
    if session.validate_result.get("passed"):
        return "finally_node"

    if session.attempt_count < session.max_attempt:
        return "coding_agent"
    return "human_intervene"


# ============================================================
# Graph Builder
# ============================================================

def _traced_node(name: str, fn):
    """节点追踪包装：service=graph_node，自动挂 turn span 之下"""
    from ..trace import get_tracer

    def wrapper(state: DotAgentState) -> dict[str, Any]:
        session = state.get("session") if isinstance(state, dict) else None
        span = get_tracer().start_span(
            "graph_node", name,
            input_summary=(getattr(session, "task", "") or "")[:80],
        )
        try:
            result = fn(state)
            if isinstance(result, dict) and result:
                span.set_output_summary(f"keys={sorted(result.keys())[:5]}")
            else:
                span.set_output_summary("state-on-session")
            span.finish()
            return result
        except BaseException as exc:
            span.finish(exc)
            raise

    return wrapper


def build_graph() -> StateGraph:
    """构建未编译的图（无 checkpointer：介入/断点机制全部自定义）"""
    graph = StateGraph(DotAgentState)

    graph.add_node("context_compress", _traced_node("context_compress", context_compress_node))
    graph.add_node("plan_node", _traced_node("plan_node", plan_node))
    graph.add_node("coding_agent", _traced_node("coding_agent", coding_agent_node))
    graph.add_node("valid_node", _traced_node("valid_node", valid_node))
    graph.add_node("human_intervene", _traced_node("human_intervene", human_intervene_node))
    graph.add_node("finally_node", _traced_node("finally_node", finally_node))

    # 固定链路
    graph.add_edge(START, "context_compress")
    graph.add_edge("context_compress", "plan_node")
    graph.add_edge("plan_node", "coding_agent")
    graph.add_edge("human_intervene", "finally_node")
    graph.add_edge("finally_node", END)

    # 条件路由
    graph.add_conditional_edges(
        "coding_agent",
        route_coding_agent,
        {
            "plan_node": "plan_node",
            "valid_node": "valid_node",
            "human_intervene": "human_intervene",
        },
    )

    graph.add_conditional_edges(
        "valid_node",
        route_valid_node,
        {
            "finally_node": "finally_node",
            "coding_agent": "coding_agent",
            "human_intervene": "human_intervene",
        },
    )

    # human_intervene → finally 固定边（介入=结束本轮，continue 由外部重新进图）
    return graph


def compile_graph():
    """构建并编译图（无 checkpointer：断点/介入机制全部自定义）"""
    return build_graph().compile()


# ============================================================
# Helpers
# ============================================================

def _run_tool_call(
        session: Session,
        runtime: Any,
        tools: list[Any],
        call: dict[str, Any],
        writer: Any,
        *,
        prefix: str,
) -> ToolMessage | None:
    """执行单个工具调用（对齐 fix.md / fix-权限控制.md）：
    0. 权限校验（最前）：三级拦截（系统黑名单→项目黑名单→模式规则）
       ASK → 控制台 Y/N 审批 → 确认后带单次标记重走完整校验
    1. mcp_ / skill_ 开头 → dispatch_special_tool 特殊处理
       （mcp_ 未加载返回定义、已加载转发执行；skill_ 返回 skill 内容）
    2. 其余系统工具 → Hook 拦截 → execute_tool_by_name
    """
    tool_name = call.get("name", "")

    # ---- 权限校验（统一入口，所有工具必经）----
    from ..core.permission import Decision, get_permission_manager

    pm = get_permission_manager()
    args = call.get("args") or {}
    agent_mode = getattr(runtime, "agent_mode", "auto") if runtime is not None else "auto"
    decision = pm.check(tool_name, args, agent_mode=agent_mode, workspace=session.workspace)

    if decision.decision is Decision.DENY:
        writer({"type": "permission_denied", "name": tool_name, "source": decision.source, "reason": decision.reason})
        return ToolMessage(
            content=decision.deny_message(),
            tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
        )

    if decision.decision is Decision.ASK:
        approved = pm.ask_user(tool_name, args, decision, agent_mode=agent_mode)
        if not approved:
            writer({"type": "permission_denied", "name": tool_name, "source": "user", "reason": "user rejected"})
            return ToolMessage(
                content="用户拒绝了本次操作的审批请求。",
                tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
            )
        # 确认后重走完整校验（黑名单依然拦截，模式层单次放行）
        decision = pm.check(
            tool_name, args, agent_mode=agent_mode, workspace=session.workspace, approved=True,
        )
        if decision.decision is Decision.DENY:
            writer(
                {"type": "permission_denied", "name": tool_name, "source": decision.source, "reason": decision.reason})
            return ToolMessage(
                content=decision.deny_message(),
                tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
            )

    # 渐进披露特殊分发（mcp_/skill_ 前缀）
    special = dispatch_special_tool(session, call)
    if special is not None:
        return special

    # PreToolUse Hook 拦截
    if session.hook_runner is not None:
        hook_result = _check_hook(
            session.hook_runner,
            session.session_id,
            session.workspace,
            tool_name,
            call.get("args") or {},
        )
        if hook_result.blocked:
            err_msg = ToolMessage(
                content=hook_result.feedback or f"Blocked by hook: {tool_name}",
                tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
            )
            writer({"type": "tool_blocked", "name": tool_name, "reason": hook_result.feedback})
            return err_msg

    try:
        return execute_tool_by_name(
            tools=tools,
            call=call,
            hook_runner=session.hook_runner,
            budget=getattr(runtime, "result_budget", None) if runtime is not None else None,
            workspace=session.workspace,
            runtime=runtime,
        )
    except Exception as exc:
        logger.warning("tool execution failed: %s", exc, exc_info=True)
        return ToolMessage(
            content=f"Error: {exc}",
            tool_call_id=call.get("id", f"{prefix}-{tool_name}"),
        )


def _check_hook(
        hook_runner: HookRunner,
        session_id: str,
        workspace: Any,
        tool_name: str,
        tool_args: dict[str, Any],
) -> HookResult:
    try:
        return hook_runner.run(
            HookEvent.PreToolUse,
            HookPayload(
                event=HookEvent.PreToolUse,
                tool_name=tool_name,
                tool_args=dict(tool_args),
                session_id=session_id,
                workspace=str(workspace) if workspace else "",
            ),
        )
    except Exception as exc:
        logger.debug("_check_hook: skipped (%s)", exc)
        return HookResult()


#
# def _evaluate_plan_quality(
#     *,
#     plan_description: str,
#     subtasks: list[dict],
#     tool_call_count: int,
#     loop_count: int,
#     summary: str,
# ) -> tuple[bool, str]:
#     """启发式 plan 质量评估（空 plan / 无产出 / 循环耗尽视为无效）"""
#     if not plan_description and not subtasks:
#         return False, "Plan is empty — no description or subtasks provided."
#     if not subtasks:
#         return False, "Plan has no subtasks — nothing to execute."
#     if loop_count >= CODING_MAX_LOOPS and tool_call_count == 0:
#         return False, "Agent exhausted all loops without making any tool calls."
#     if not summary or summary == "(no result)":
#         return False, "Agent produced no output after execution."
#     return True, ""

import json
from typing import Any, Tuple


def _evaluate_plan_quality(
        *,
        plan_description: str,
        subtasks: list[dict],
        tool_call_count: int,
        loop_count: int,
        summary: str,
) -> Tuple[bool, str]:
    """
    LLM驱动评估计划执行质量。
    兜底：完全空计划直接判定无效；其余交给LLM判断是否需要重规划。
    返回 (is_reasonable: bool, issue: str)
    """
    # 兜底：完全空计划，不走LLM，节省token
    if not plan_description and not subtasks:
        return False, "Plan is empty — no description or subtasks provided."

    eval_system = """
You are a plan‑execution evaluator.
Given original plan, execution statistics and agent output, judge whether the task execution is reasonable and finished.

Rules:
1. Return ONLY JSON object, no markdown, no extra text.
2. Output schema:
{
  "is_reasonable": boolean,
  "issue": string
}
- is_reasonable = true: execution matches plan, task is completed or can proceed to validation node.
- is_reasonable = false: execution deviates from plan, stuck, meaningless loop, failed to complete subtasks → need replan.
- issue: when is_reasonable=false, write concise reason; otherwise empty string.

Evaluate from these dimensions:
- Whether the agent followed the subtasks in the plan.
- Whether stuck in useless loops, zero effective tool calls.
- Whether output answers user request / coding objective.
- For simple chat‑only tasks: no tool calls is acceptable, as long as meaningful answer produced.
"""

    eval_user_prompt = f"""
=== Original Plan ===
plan_description: {plan_description}
subtasks: {json.dumps(subtasks, ensure_ascii=False)}

=== Execution Statistic ===
loop_count: {loop_count}
tool_call_count: {tool_call_count}

=== Agent Final Output Summary ===
summary: {summary}

Please evaluate this plan execution.
"""
    messages = [
        {"role": "system", "content": eval_system},
        {"role": "user", "content": eval_user_prompt},
    ]

    try:
        # 调用现有统一LLM入口，node标记为plan_evaluate，不使用tools
        resp = _invoke_llm(messages, node="plan_evaluate", tools=None)
        raw_content = getattr(resp, "content", "")
        parsed = json.loads(raw_content.strip())
        is_reasonable = bool(parsed.get("is_reasonable", True))
        issue = str(parsed.get("issue", ""))
        return is_reasonable, issue

    except Exception as e:
        # LLM调用失败 / JSON解析失败，兜底放行，避免整个工作流卡死
        logger.warning("_evaluate_plan_quality llm evaluate failed, fallback pass: %s", e, exc_info=True)
        return True, ""


def _parse_validation_result(messages: list[Any]) -> dict[str, Any]:
    """从最后的 AI 消息解析校验 JSON"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = _extract_json_text(str(getattr(msg, "content", "") or ""))
            if text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return {
                            "passed": bool(parsed.get("passed", False)),
                            "error_msg": str(parsed.get("reason", "")),
                            "fail_reason": str(parsed.get("reason", "")),
                            "checks": parsed.get("checks", []),
                        }
                except json.JSONDecodeError:
                    continue
    return {
        "passed": False,
        "error_msg": "Verifier did not return valid JSON.",
        "fail_reason": "Verifier did not return valid JSON.",
        "checks": [],
    }


def _extract_json_text(text: str) -> str:
    """剥 markdown 围栏，截取第一个 { 起的 JSON 文本"""
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start == -1:
        return ""
    return text[start:]


def _load_static_context(workspace: Path) -> str:
    """加载静态上下文：.dot/memory/dot.md（用户行为偏好文件，对齐 fix.md）"""
    return _load_file_safe(workspace / ".dot" / "memory" / "dot.md")


def _load_file_safe(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _safe_host_call(host: Any, method: str, default: str, *args: Any) -> str:
    """安全调用 host 方法，失败返回默认值"""
    if host is None:
        return default
    try:
        result = getattr(host, method)(*args)
        return result if isinstance(result, str) else default
    except Exception as exc:
        logger.debug("host call %s failed: %s", method, exc)
        return default


def _last_final_answer(session: Session) -> str:
    """生成最终答复文本"""
    passed = session.validate_result.get("passed", False)
    status = "PASSED" if passed else ("STOPPED" if session.resume_action == "stop" else "FAILED")
    summary = session.task_plan.get("description", "") or session.task
    body = last_ai_content(session.messages[-8:]) if session.messages else ""
    answer = f"[{status}] {summary}"
    if body:
        answer += f"\n\n{body[:2000]}"
    return answer


def _short_args(args: Any) -> str:
    """截断工具参数用于日志"""
    if not args:
        return ""
    text = str(args)
    return text[:120] + ("..." if len(text) > 120 else "")
