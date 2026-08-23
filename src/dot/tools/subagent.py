"""
极简同步 Fork 子Agent系统（对标 Claude Code 同步 SubAgent）

核心设计：
  - 父Agent调用 spawn_subagent → 创建全新独立子Agent实例
  - 子Agent拥有独立 messages、独立 ReAct 循环，上下文完全隔离
  - 子Agent内部所有思考、工具调用、中间日志不回传父上下文
  - 子任务结束后仅 LLM 生成的精简摘要回传给父
  - 父阻塞等待子完成（同步前台模式）

模块结构：
  - SubAgentRequest   — 子Agent请求参数
  - SubAgentState     — 子Agent内部状态（隔离于父）
  - run_subagent()    — 子Agent独立 ReAct 执行循环
  - generate_summary()— LLM 生成精简摘要
  - build_subagent_tool() — 构建 StructuredTool 注册给父Agent
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from ..core.llm import create_model
from ..core.log import get_logger
from ..core.utils import last_ai_content

logger = get_logger(__name__)

# 子Agent默认配置
SUBAGENT_MAX_LOOPS = 15       # 最大 ReAct 迭代次数
SUBAGENT_TIMEOUT_SECONDS = 300  # 最大执行时间（秒）

# 子Agent系统提示词
_SUBAGENT_SYSTEM_PROMPT = """\
你是一个专注执行子任务的子代理。你的职责：
1. 严格按用户给定的任务描述执行，完成任务后给出清晰结论。
2. 你可以使用提供的所有工具来完成任务。
3. 执行过程中保持专注，不要偏离任务目标。
4. 完成后用一段简洁的文字总结你的执行结果。
"""

# 摘要生成提示词
_SUMMARY_PROMPT = """\
请你对本次子代理的全部执行过程生成精简摘要，仅输出给父代理使用，包含：
1. 本次执行的所有操作（简述）
2. 关键发现与数据结果
3. 最终结论与可交付内容

禁止输出原始工具日志、原始对话、中间思考，只保留核心有效信息。
请直接输出摘要文本，不要加任何前缀或格式标记。
"""


@dataclass
class SubAgentRequest:
    """子Agent请求参数（父传入）"""
    task_prompt: str                    # 子任务描述
    max_loops: int = SUBAGENT_MAX_LOOPS
    timeout_seconds: int = SUBAGENT_TIMEOUT_SECONDS


@dataclass
class SubAgentState:
    """子Agent内部状态（完全隔离于父）

    独立 messages、独立执行状态，绝不与父共享引用。
    """
    messages: list[Any] = field(default_factory=list)
    done: bool = False
    summary: str = ""
    tool_call_count: int = 0
    loop_count: int = 0
    error: str = ""
    start_time: float = field(default_factory=time.time)


def run_subagent(
    request: SubAgentRequest,
    *,
    parent_session: Any,  # Session，用于继承 workspace/tools/hook 等设施
) -> str:
    """执行子Agent（同步阻塞）

    完全隔离的执行流程：
    1. 创建独立 SubAgentState（独立 messages）
    2. 构建子Agent可调用的工具集（从父 session 继承）
    3. ReAct 循环：思考 → 工具调用 → 迭代
    4. 生成精简摘要
    5. 仅摘要字符串返回父，子 messages 直接销毁

    Args:
        request: 子Agent请求参数
        parent_session: 父 Session（仅用于读取 workspace/tools 等共享设施，不写入）

    Returns:
        str: 精简摘要文本
    """
    state = SubAgentState()
    logger.info(
        "[subagent] starting: task=%r, max_loops=%d, timeout=%ds",
        request.task_prompt[:80], request.max_loops, request.timeout_seconds,
    )

    # 构建子Agent工具集（从父继承，不共享 state）
    tools = _build_subagent_tools(parent_session)

    # 子Agent独立系统提示 + 任务
    state.messages.append(SystemMessage(content=_SUBAGENT_SYSTEM_PROMPT))
    state.messages.append(HumanMessage(content=request.task_prompt))

    # ReAct 循环（独立于父）
    try:
        _react_loop(state, request, tools, parent_session)
    except _SubAgentTimeout:
        state.error = "子Agent执行超时"
        logger.warning("[subagent] timeout after %ds", request.timeout_seconds)
    except Exception as exc:
        state.error = f"子Agent执行异常: {exc}"
        logger.warning("[subagent] unexpected error: %s", exc, exc_info=True)

    # 生成摘要（单独一次 LLM 调用）
    state.summary = generate_summary(state, parent_session)
    state.done = True

    logger.info(
        "[subagent] done: loops=%d, tool_calls=%d, summary_len=%d, error=%s",
        state.loop_count, state.tool_call_count,
        len(state.summary), state.error or "(none)",
    )
    return state.summary


def _react_loop(
    state: SubAgentState,
    request: SubAgentRequest,
    tools: list[Any],
    parent_session: Any,
) -> None:
    """子Agent私有 ReAct 循环（完全封闭在子内部）"""
    # Model 只创建一次，复用所有循环（避免每轮重复创建 + bind_tools）
    model = create_model()
    if tools:
        model = model.bind_tools(tools)

    while state.loop_count < request.max_loops:
        # 超时检查
        elapsed = time.time() - state.start_time
        if elapsed > request.timeout_seconds:
            raise _SubAgentTimeout()

        state.loop_count += 1

        try:
            response = model.invoke(state.messages)
        except Exception as exc:
            logger.warning("[subagent] LLM invoke failed at loop %d: %s", state.loop_count, exc)
            state.messages.append(AIMessage(content=f"[LLM Error] {exc}"))
            break

        state.messages.append(response)

        # 解析工具调用
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            logger.info("[subagent] loop=%d: no tool calls, agent finished", state.loop_count)
            break

        state.tool_call_count += len(tool_calls)
        logger.info("[subagent] loop=%d: %d tool calls", state.loop_count, len(tool_calls))

        # 执行工具调用（子独立执行，不影响父）
        for call in tool_calls:
            tool_msg = _run_subagent_tool_call(tools, call, parent_session)
            if tool_msg is not None:
                state.messages.append(tool_msg)


def _run_subagent_tool_call(
    tools: list[Any],
    call: dict[str, Any],
    parent_session: Any,
) -> ToolMessage | None:
    """执行子Agent的单个工具调用

    直接调用工具（不走 execute_tool_by_name），避免污染父 session 的
    _message_seq 计数器。Hook 仍通过父 hook_runner 执行（安全约束）。
    """
    tool_name = call.get("name", "")
    tool_call_id = call.get("id", f"sub-{tool_name}")
    args = call.get("args") or {}

    logger.info("[subagent:tool] -> %s  args=%s", tool_name, _short_args(args))

    def _tm(payload: dict[str, Any]) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            name=tool_name,
            tool_call_id=tool_call_id,
        )

    # PreToolUse Hook（继承父的安全约束）
    hook_runner = getattr(parent_session, "hook_runner", None)
    if hook_runner is not None:
        try:
            from ..core.hooks import HookEvent, HookPayload
            hook_result = hook_runner.run(
                HookEvent.PreToolUse,
                HookPayload(
                    event=HookEvent.PreToolUse,
                    tool_name=tool_name,
                    tool_args=dict(args),
                    session_id=getattr(parent_session, "session_id", ""),
                    workspace=str(getattr(parent_session, "workspace", "")),
                ),
            )
            if hook_result.blocked:
                return _tm({"ok": False, "error": hook_result.feedback or f"blocked by hook: {tool_name}"})
            if hook_result.updated_args is not None:
                args = hook_result.updated_args
        except Exception as exc:
            logger.debug("[subagent:tool] hook skipped: %s", exc)

    # 查找并执行工具
    tools_map = {t.name: t for t in tools}
    tool = tools_map.get(tool_name)
    if tool is None:
        return _tm({"ok": False, "error": f"unknown tool: {tool_name}"})

    try:
        result = tool.invoke(args)
    except Exception as exc:
        logger.warning("[subagent:tool] %s failed: %s", tool_name, exc, exc_info=True)
        return _tm({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    content = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)
    logger.info("[subagent:tool] %s result: %s", tool_name, content[:300])
    return _tm(result if isinstance(result, dict) else {"ok": True, "result": result})


def generate_summary(state: SubAgentState, parent_session: Any) -> str:
    """子Agent专属摘要生成（核心关键）

    单独调用一次 LLM，基于子完整执行过程生成给父Agent看的精简结构化摘要。
    如果 LLM 调用失败，降级为截取最后一条 AI 消息。
    """
    if state.error and not state.tool_call_count:
        # 未执行任何工具就出错了，直接返回错误信息
        return f"[子Agent异常] {state.error}"

    # 收集子执行过程的关键信息用于摘要
    _execution_context = _build_execution_context(state)

    summary_messages = [
        SystemMessage(content=_SUMMARY_PROMPT),
        HumanMessage(content=_execution_context),
    ]

    try:
        model = create_model()
        response = model.invoke(summary_messages)
        content = str(getattr(response, "content", "") or "").strip()
        if content:
            logger.info("[subagent:summary] generated (%d chars)", len(content))
            return content
    except Exception as exc:
        logger.warning("[subagent:summary] LLM failed, fallback: %s", exc)

    # 降级：取最后一条 AI 消息
    fallback = last_ai_content(state.messages) or state.error or "子Agent未产生有效输出"
    logger.info("[subagent:summary] fallback (%d chars)", len(fallback))
    return fallback


def _build_execution_context(state: SubAgentState) -> str:
    """从子Agent消息中提取摘要上下文（过滤中间日志，只保留关键信息）"""
    parts: list[str] = []
    parts.append(f"=== 子Agent执行统计 ===")
    parts.append(f"ReAct循环次数: {state.loop_count}")
    parts.append(f"工具调用总数: {state.tool_call_count}")
    parts.append(f"执行耗时: {time.time() - state.start_time:.1f}s")
    if state.error:
        parts.append(f"错误信息: {state.error}")

    # 提取工具调用摘要（只保留工具名+关键结果片段）
    parts.append(f"\n=== 工具调用记录 ===")
    for msg in state.messages:
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                parts.append(f"- 调用: {tc.get('name', '?')}")
        elif isinstance(msg, ToolMessage):
            content = str(getattr(msg, "content", "") or "")
            # 截断工具结果，只保留前200字符
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"  结果: {content}")

    # 最后一条 AI 输出（子的最终回答）
    last_ai = last_ai_content(state.messages)
    if last_ai:
        parts.append(f"\n=== 子Agent最终输出 ===")
        parts.append(last_ai[:2000])

    return "\n".join(parts)


def _build_subagent_tools(parent_session: Any) -> list[Any]:
    """为子Agent构建工具集（从父 session 继承完整工具集）

    使用 build_tools_for_session 而非 build_tools，确保子Agent也能使用
    MCP/Skill 元工具（mcp_search / skill_search）。
    """
    from .meta import build_tools_for_session
    try:
        return build_tools_for_session(parent_session)
    except Exception as exc:
        logger.warning("[subagent] tool build failed: %s", exc, exc_info=True)
        return []


class _SubAgentTimeout(Exception):
    """子Agent超时异常（内部使用）"""
    pass


def _short_args(args: Any) -> str:
    """截断工具参数用于日志"""
    if not args:
        return ""
    text = str(args)
    return text[:120] + ("..." if len(text) > 120 else "")


# ============================================================
# 工具注册
# ============================================================

def build_subagent_tool(session: Any) -> StructuredTool:
    """构建 spawn_subagent 工具（注册给父Agent使用）

    父Agent通过调用此工具派生子Agent，同步阻塞等待子完成，
    仅收到子的精简摘要文本。
    """
    def spawn_subagent(task_prompt: str, max_loops: int = SUBAGENT_MAX_LOOPS) -> str:
        """派生一个子代理执行指定子任务，同步阻塞等待完成后返回精简摘要。

        Args:
            task_prompt: 子任务的完整描述（越详细越好，子代理将独立执行）
            max_loops: 子代理最大迭代次数（默认15，防止死循环）

        Returns:
            子代理执行完成后的精简摘要文本
        """
        request = SubAgentRequest(
            task_prompt=task_prompt,
            max_loops=min(max_loops, 30),  # 硬上限保护
        )
        return run_subagent(request, parent_session=session)

    return StructuredTool.from_function(
        name="spawn_subagent",
        func=spawn_subagent,
        description=(
            "派生一个独立子代理（同步阻塞执行）。"
            "子代理拥有独立上下文和独立工具执行能力，执行完成后返回精简摘要。"
            "适用场景：需要独立调研、文件分析、局部执行等可拆分的子任务。"
            "子代理的中间过程不会污染父上下文，只返回最终结果摘要。"
            "注意：调用后会阻塞等待子代理完成，请合理拆分任务粒度。"
        ),
    )
