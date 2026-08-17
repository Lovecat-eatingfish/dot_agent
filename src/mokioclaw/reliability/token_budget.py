"""
Token 预算追踪器 + 输出截断恢复（对齐 Claude Code）

- BudgetTracker: 追踪每轮 token 消耗，达 90% 预算停止；检测收益递减（连续 3 次增量 < 阈值）
- max_output_tokens 恢复：finish_reason=length 时升级 max_tokens（8k→64k）重试，
  升级后仍截断则注入 "Resume directly" meta 消息，最多 3 次
- 413 prompt-too-long 恢复：捕获上下文超长异常 → force_compact（L4）→ 重试，最多 1 次

子 Agent 不参与 token 预算（对齐 Claude Code：由父级 maxTurns 控制）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 预算检查阈值
COMPLETION_THRESHOLD = 0.9          # 已用 token 达预算 90% → 停止
DIMINISHING_THRESHOLD = 500         # 连续增量 < 此值 → 收益递减
DIMINISHING_MIN_CONTINUATIONS = 3   # 收益递减判定需至少 3 次 continuation

# max_output_tokens 恢复
DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 64_000
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3

# 413 恢复
MAX_PROMPT_TOO_LONG_RETRIES = 1

# 收益递减 / 预算停止时注入的引导消息
NUDGE_MESSAGE = (
    "Continue working on the task. Use tools directly without preamble."
)

# 输出截断后注入的恢复消息（对齐 Claude Code "no apology, no recap"）
OUTPUT_RESUME_MESSAGE = (
    "Output token limit hit. Resume directly — no apology, no recap. "
    "Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


@dataclass
class BudgetTracker:
    """输出 token 预算追踪器（对齐 Claude Code BudgetTracker）

    continuation_count: 连续无工具调用的轮数
    last_delta_tokens: 上一次检查时的增量
    last_global_turn_tokens: 上一次检查时的累计 token
    started: 是否已开始追踪
    """

    continuation_count: int = 0
    last_delta_tokens: int = 0
    last_global_turn_tokens: int = 0
    started: bool = False
    # 累计输出 token（跨轮）
    total_output_tokens: int = 0
    # 预算上限（None 表示不限制）
    budget: int | None = None

    def account(self, response: Any) -> int:
        """从模型 response 提取本轮输出 token 并累加，返回本轮 output_tokens"""
        output_tokens = _extract_output_tokens(response)
        self.total_output_tokens += output_tokens
        return output_tokens

    def check(self) -> tuple[bool, str]:
        """检查是否应停止循环

        Returns:
            (should_stop, reason)。should_stop=True 时调用方应 break。
            子 Agent（budget=None）永远返回 (False, "")。
        """
        if self.budget is None or self.budget <= 0:
            return False, ""

        turn_tokens = self.total_output_tokens
        if not self.started:
            self.started = True
            self.last_global_turn_tokens = turn_tokens
            return False, ""

        delta = turn_tokens - self.last_global_turn_tokens

        # 收益递减：本次 + 上次增量都小于阈值（对齐 Claude Code）
        is_diminishing = (
            self.continuation_count >= DIMINISHING_MIN_CONTINUATIONS
            and delta < DIMINISHING_THRESHOLD
            and self.last_delta_tokens < DIMINISHING_THRESHOLD
        )

        # 更新基线：先记下"上次 delta"再覆盖"本次 delta"
        self.last_delta_tokens = delta
        self.last_global_turn_tokens = turn_tokens

        if is_diminishing:
            return True, "diminishing_returns"

        # 达预算阈值
        if turn_tokens >= self.budget * COMPLETION_THRESHOLD:
            return True, "budget_threshold"

        self.continuation_count += 1
        return False, ""


@dataclass
class OutputTokenRecovery:
    """max_output_tokens 截断恢复状态机（对齐 Claude Code 两阶段恢复）

    阶段一：升级 max_tokens（8k → 64k）重试
    阶段二：升级后仍截断 → 注入 OUTPUT_RESUME_MESSAGE meta 消息，最多 3 次
    """

    max_output_tokens_override: int | None = None
    recovery_count: int = 0
    escalated: bool = False

    def on_truncated(self) -> dict[str, Any] | None:
        """检测到 finish_reason=length 时调用，返回恢复动作

        Returns:
            None: 不再恢复（已达上限），调用方应 break
            {"action": "escalate"}: 升级 max_tokens，调用方用新 override 重试（不 break）
            {"action": "resume", "message": str}: 注入 resume 消息后继续（不 break）
        """
        # 阶段一：尚未升级 → 升级
        if not self.escalated:
            self.escalated = True
            self.max_output_tokens_override = ESCALATED_MAX_TOKENS
            logger.info("max_output_tokens recovery: escalate to %d", ESCALATED_MAX_TOKENS)
            return {"action": "escalate"}

        # 阶段二：已升级仍截断 → 注入 resume 消息
        if self.recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            self.recovery_count += 1
            logger.info("max_output_tokens recovery: inject resume message (%d/%d)",
                        self.recovery_count, MAX_OUTPUT_TOKENS_RECOVERY_LIMIT)
            return {"action": "resume", "message": OUTPUT_RESUME_MESSAGE}

        # 恢复次数用尽
        return None

    def reset_for_new_turn(self) -> None:
        """新的一轮（非截断路径）重置 resume 计数，但保留 escalated 状态"""
        self.recovery_count = 0


@dataclass
class PromptTooLongRecovery:
    """413 prompt-too-long 恢复状态（对齐 Claude Code reactive compact 链）

    捕获上下文超长异常 → force_compact（L4）→ 重试，最多 1 次。
    """

    attempted: bool = False

    def should_recover(self) -> bool:
        return not self.attempted

    def mark_attempted(self) -> None:
        self.attempted = True


def is_truncated(response: Any) -> bool:
    """判断模型输出是否因 max_tokens 被截断"""
    meta = getattr(response, "response_metadata", None) or {}
    finish_reason = str(meta.get("finish_reason") or "").lower()
    return finish_reason == "length"


def _extract_output_tokens(response: Any) -> int:
    """从 AIMessage 提取 output_tokens（兼容 usage_metadata / response_metadata）"""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        out = usage.get("output_tokens")
        if isinstance(out, int) and out > 0:
            return out
        total = usage.get("total_tokens")
        inp = usage.get("input_tokens")
        if isinstance(total, int) and isinstance(inp, int) and total > inp:
            return total - inp
    meta = getattr(response, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict):
        out = token_usage.get("output_tokens") or token_usage.get("completion_tokens")
        if isinstance(out, int) and out > 0:
            return out
    return 0


def is_prompt_too_long_error(exc: Exception) -> bool:
    """判断异常是否为上下文超长（413 / context_length_exceeded）"""
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "context_length",
        "context window",
        "prompt is too long",
        "maximum context length",
        "too long",
        "413",
    )
    return any(m in text for m in markers)


def model_with_max_tokens(model: Any, max_tokens: int | None) -> Any:
    """给 model 覆盖 max_tokens（返回新实例，不修改原 model）

    ChatOpenAI 用 max_tokens 作为 max_completion_tokens 的 alias，.bind(max_tokens=)
    透传后由 ChatOpenAI 在请求构建阶段重命名为 max_completion_tokens。
    同时传 max_completion_tokens 覆盖 alias 字段，兼容旧版/新版 langchain_openai
    以及非 OpenAI 兼容端点。
    """
    if max_tokens is None:
        return model
    try:
        return model.bind(max_tokens=max_tokens, max_completion_tokens=max_tokens)
    except Exception:
        return model


def filter_unresolved_tool_uses(messages: list[Any]) -> list[Any]:
    """清洗悬空的 tool_use：为没有对应 tool_result 的 tool_call 补占位 result

    对齐 Claude Code yieldMissingToolResultBlocks / filterUnresolvedToolUses。
    场景：Agent 在工具执行中途被中断、max_loops 跳出、异常路径退出，
    导致 AIMessage 带 tool_calls 但后面缺对应 ToolMessage → API 返回 400。

    策略：扫描最后一条 assistant 之后的 tool_use id，若无匹配 tool_result 则补一条
    is_error 占位 ToolMessage。保留原有合法配对不动。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if not messages:
        return list(messages)

    result = list(messages)

    # 收集每条 AIMessage 的 tool_call id 与其后是否存在对应 ToolMessage
    # 只处理"末尾悬空"——即某 AIMessage 的 tool_call 在其后无 ToolMessage 配对
    pending_tool_uses: list[tuple[str, str]] = []  # (tool_call_id, tool_name)
    resolved_ids: set[str] = set()

    for msg in result:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                if tc_id and tc_id not in resolved_ids:
                    pending_tool_uses.append((str(tc_id), str(name or "tool")))
        elif isinstance(msg, ToolMessage):
            tid = getattr(msg, "tool_call_id", None)
            if tid:
                resolved_ids.add(str(tid))

    # 仍 pending 的 = 末尾悬空 tool_use
    unresolved = [
        (tid, name) for tid, name in pending_tool_uses if tid not in resolved_ids
    ]
    if not unresolved:
        return result

    for tid, name in unresolved:
        placeholder = ToolMessage(
            content="[Tool result missing due to internal error]",
            name=name,
            tool_call_id=tid,
        )
        result.append(placeholder)
        logger.debug("filter_unresolved_tool_uses: injected placeholder for %s/%s", name, tid)

    return result


def make_pairing_safe(messages: list[Any]) -> list[Any]:
    """压缩/切片后修复 AIMessage(tool_calls) 与 ToolMessage 的配对（共享出口）

    1. 丢弃孤儿 ToolMessage（父 AIMessage 被切掉、前文无对应 tool_call_id）
    2. 用 filter_unresolved_tool_uses 为末尾悬空 tool_call 补占位 result

    所有 messages[-keep_last:] 式切片的出口（reactive/force/dual-threshold 压缩、
    agent_loop 应急兜底）都必须过这个函数——不修复配对直接发给 API 会返回 400
    （tool message 必须紧跟对应 tool_calls）。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    seen_call_ids: set[str] = set()
    cleaned: list[Any] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    seen_call_ids.add(str(tc_id))
            cleaned.append(msg)
        elif isinstance(msg, ToolMessage):
            if str(getattr(msg, "tool_call_id", "") or "") in seen_call_ids:
                cleaned.append(msg)
        else:
            cleaned.append(msg)
    return filter_unresolved_tool_uses(cleaned)
