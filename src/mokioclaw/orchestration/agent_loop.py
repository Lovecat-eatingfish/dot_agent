"""
通用 Agent 工具调用循环

提供 run_agent_loop 函数，封装 "调用模型 → 检查工具调用 → 执行工具 → 循环" 的通用模式。
独立只读工具自动并行；mutating 工具串行（见 reliability.parallel）。

对齐 Claude Code 引擎层恢复链：
- token 预算追踪（BudgetTracker）：达 90% 预算 / 收益递减 → 停止
- max_output_tokens 恢复：finish_reason=length → 升级 max_tokens（8k→64k）→ 注入 resume 消息
- prompt-too-long 恢复：413 异常 → force_compact（L4）→ 重试
- 悬空 tool_use 清洗：max_loops 跳出时补占位 ToolMessage，防 API 400
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import execute_tool_calls, last_ai_content
from mokioclaw.reliability.cost import record_llm_usage
from mokioclaw.reliability.token_budget import (
    BudgetTracker,
    OutputTokenRecovery,
    PromptTooLongRecovery,
    filter_unresolved_tool_uses,
    is_prompt_too_long_error,
    is_truncated,
    model_with_max_tokens,
    NUDGE_MESSAGE,
)

logger = get_logger(__name__)

# 工具执行器类型：接收一个 tool_call 字典，返回 ToolMessage
ToolExecutor = Callable[[dict[str, Any]], ToolMessage]


def run_agent_loop(
    model_with_tools: Any,
    messages: list[Any],
    *,
    tool_executor: ToolExecutor,
    max_loops: int = 8,
    stop_message: str = "stopped after the maximum tool loop count.",
    max_workers: int = 4,
    parallel: bool = True,
    token_budget: int | None = None,
    workspace: Any = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """执行通用的 Agent 工具调用循环

    Args:
        model_with_tools: 已绑定工具的 LangChain 模型
        messages: 初始消息列表（会原地追加）
        tool_executor: 工具执行回调，接收 tool_call dict，返回 ToolMessage
        max_loops: 最大循环次数
        stop_message: 达到最大循环时的提示信息
        max_workers: 并行工具上限
        parallel: 是否启用并行调度
        token_budget: 输出 token 预算上限（None=不限制，子 Agent 默认不限制）
        workspace: 工作区路径（413 恢复时 force_compact 需要）

    Returns:
        (produced_messages, tool_events) 元组
    """
    produced_messages: list[Any] = []
    budget = BudgetTracker(budget=token_budget)
    output_recovery = OutputTokenRecovery()
    prompt_recovery = PromptTooLongRecovery()

    current_model = model_with_tools

    loops_done = 0
    while loops_done < max_loops:
        loops_done += 1

        # ===== 调用模型，带 prompt-too-long 恢复 =====
        response = _invoke_with_recovery(
            current_model,
            messages,
            prompt_recovery,
            workspace=workspace,
        )
        if response is None:
            # 恢复失败，force_compact 已尽 → 跳出
            break

        # ===== usage 记录（/cost 美元统计） =====
        record_llm_usage(response)

        produced_messages.append(response)
        messages.append(response)

        # ===== token 预算追踪 =====
        accounted_tokens = budget.account(response)

        # ===== max_output_tokens 截断检测与恢复 =====
        if is_truncated(response):
            recovery = output_recovery.on_truncated()
            if recovery is None:
                # 恢复次数用尽，跳出
                break
            if recovery["action"] == "escalate":
                current_model = model_with_max_tokens(model_with_tools, output_recovery.max_output_tokens_override)
                # 不 append tool_calls 处理，直接重试同一轮（回退一条以重试）
                messages.pop()
                produced_messages.pop()
                # 回滚本轮已计入的 token，避免污染预算基线
                budget.total_output_tokens -= accounted_tokens
                loops_done -= 1
                continue
            if recovery["action"] == "resume":
                resume_msg = HumanMessage(content=recovery["message"])
                messages.append(resume_msg)
                produced_messages.append(resume_msg)
                continue

        # ===== 预算检查：达阈值或收益递减 → 停止 =====
        should_stop, reason = budget.check()
        if should_stop:
            logger.info("agent loop stopping: %s (budget=%s, used=%d)",
                        reason, token_budget, budget.total_output_tokens)
            produced_messages.append(HumanMessage(content=NUDGE_MESSAGE))
            break

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        # 正常进入下一轮前重置截断恢复计数
        output_recovery.reset_for_new_turn()

        if parallel and len(tool_calls) > 1:
            results = execute_tool_calls(tool_calls, tool_executor, max_workers=max_workers)
        else:
            results = [tool_executor(call) for call in tool_calls]

        for tool_message in results:
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content=stop_message))

    # ===== 悬空 tool_use 清洗：循环跳出时补占位，防 API 400 =====
    # 只对即将发回调用方的 produced_messages 做清洗（messages 已被 LangGraph add_messages 管理）
    produced_messages = filter_unresolved_tool_uses(produced_messages)

    return produced_messages, []


def _invoke_with_recovery(
    model: Any,
    messages: list[Any],
    prompt_recovery: PromptTooLongRecovery,
    *,
    workspace: Any = None,
) -> Any | None:
    """调用模型，捕获 413 prompt-too-long → force_compact → 重试一次

    主模型降级回退由 invoke_with_fallback 处理（若配置了 MODEL_FALLBACKS）。
    压缩结果回写 messages（#4）：对齐 Claude Code reactive compact 持久化替换上下文，
    避免下一轮继续用膨胀列表导致二次超长不可恢复。
    """
    from mokioclaw.providers.openai_provider import invoke_with_fallback

    try:
        return invoke_with_fallback(model, messages)
    except Exception as exc:
        if not is_prompt_too_long_error(exc):
            raise
        logger.warning("prompt too long, attempting force_compact recovery: %s", exc)
        if not prompt_recovery.should_recover():
            # 已经恢复过一次仍失败 → 放弃
            logger.error("prompt-too-long recovery exhausted")
            return None
        compacted = _force_compact_messages(messages, workspace=workspace)
        # 回写压缩结果到原 messages 列表（原地替换），使后续轮次用压缩后的上下文
        messages.clear()
        messages.extend(compacted)
        prompt_recovery.mark_attempted()
        try:
            return invoke_with_fallback(model, compacted)
        except Exception as exc2:
            if is_prompt_too_long_error(exc2):
                logger.error("prompt still too long after force_compact")
                return None
            raise


def _force_compact_messages(messages: list[Any], *, workspace: Any = None) -> list[Any]:
    """L4 应急压缩：保留 system + 最近消息，中间折叠为摘要占位

    对齐 Claude Code reactive compact 兜底。复用 microcompact.force_compact_messages。
    """
    try:
        from mokioclaw.memory.microcompact import force_compact_messages

        return force_compact_messages(messages, keep_last=10)
    except Exception as exc:
        logger.debug("force_compact failed, returning last 10 messages: %s", exc)
        # 兜底：只留最后 10 条 + 第一条 system（过配对清洗，孤儿 ToolMessage 会触发 API 400）
        from mokioclaw.reliability.token_budget import make_pairing_safe

        head = [m for m in messages[:1] if m.__class__.__name__ == "SystemMessage"]
        return make_pairing_safe(head + list(messages[-10:]))
