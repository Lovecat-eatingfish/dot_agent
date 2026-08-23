"""
context_compress_node — 上下文压缩图节点

替代 coding_graph.py 中的旧实现，使用完整的三级压缩体系。
流程：
  1. 创建 ContextBudgetAllocator，估算 token
  2. 创建 CompressionPlanner，决定压缩级别
  3. 按级别执行压缩（L1 → L2 → L3）
  4. 更新 session.messages 和 session.compression_state
  5. 触发 PreCompact Hook
  6. 通过 writer 发送 context_compressed 事件
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage

from ..core.hooks import HookEvent, HookPayload
from ..core.log import get_logger
from .budget import ContextBudgetAllocator
from .l1_extract import extract_key_facts
from .l2_summarize import summarize_window
from .l3_semantic import semantic_compress
from .planner import CompressionPlanner
from .state import CompressionState

logger = get_logger(__name__)

# 默认 context window（tokens）
DEFAULT_CONTEXT_WINDOW = 128_000


def context_compress_node(state: dict[str, Any]) -> dict[str, Any]:
    """上下文压缩节点

    检测 messages 是否超限，超限则按 L1/L2/L3 策略压缩。
    直接替换 session.messages。触发前发 PreCompact Hook。
    """
    session = state["session"]
    writer = _get_writer()
    messages = list(session.messages)

    # 初始化压缩状态（如果不存在）
    if not hasattr(session, "compression_state") or session.compression_state is None:
        session.compression_state = CompressionState()

    # 创建预算分配器
    context_window = _get_context_window(session)
    budget = ContextBudgetAllocator(context_window=context_window)

    # 估算当前 token 数
    current_tokens = budget.estimate_tokens(messages)
    logger.info(
        "[node:context_compress] messages=%d, tokens=%d, threshold=%d",
        len(messages), current_tokens, budget.compression_threshold,
    )

    # 决策：是否需要压缩
    planner = CompressionPlanner(
        budget_allocator=budget,
        compression_state=session.compression_state,
        current_turn=session.current_turn_id,
    )
    decision = planner.decide(current_tokens)

    if not decision.needs_compression:
        logger.info("[node:context_compress] no compression needed: %s", decision.reason)
        return {}

    logger.info(
        "[node:context_compress] compression needed: level=%s, reason=%s",
        decision.level, decision.reason,
    )

    # PreCompact Hook
    _fire_precompact_hook(session, trigger="auto")

    # 执行压缩
    compressed_messages = messages
    l1_facts = ""
    l2_summary = ""

    # L2 滑窗摘要（先执行，因为 L2 会改变消息结构）
    if decision.needs_l2:
        compressed_messages, l2_summary = summarize_window(compressed_messages, session)
        session.compression_state.update_trigger_turn("L2", session.current_turn_id)

    # L3 语义压缩
    if decision.needs_l3:
        target_tokens = budget.target_after_compression
        compressed_messages = semantic_compress(
            compressed_messages, target_tokens, budget.estimate_tokens, session,
        )
        session.compression_state.update_trigger_turn("L3", session.current_turn_id)

    # L1 关键事实提取（从被裁剪的消息中提取）
    if decision.needs_l1:
        # 找出被裁剪的消息
        original_set = set(id(m) for m in messages)
        compressed_set = set(id(m) for m in compressed_messages)
        removed_msgs = [m for m in messages if id(m) not in compressed_set]
        if removed_msgs:
            l1_facts = extract_key_facts(removed_msgs, session)
            if l1_facts:
                # 注入到 system prompt（在第一条 system message 中追加）
                compressed_messages = _inject_l1_facts(compressed_messages, l1_facts)

    # 记录压缩历史
    after_tokens = budget.estimate_tokens(compressed_messages)
    summary_text = l2_summary or l1_facts or ""
    session.compression_state.record(
        level=decision.level,
        trigger="auto",
        before_messages=len(messages),
        after_messages=len(compressed_messages),
        before_tokens=current_tokens,
        after_tokens=after_tokens,
        summary=summary_text,
    )

    # 更新 session.messages
    session.messages = compressed_messages

    logger.info(
        "[node:context_compress] compressed %d → %d messages, %d → %d tokens (level=%s)",
        len(messages), len(compressed_messages), current_tokens, after_tokens, decision.level,
    )
    writer({
        "type": "context_compressed",
        "level": decision.level,
        "before_messages": len(messages),
        "after_messages": len(compressed_messages),
        "before_tokens": current_tokens,
        "after_tokens": after_tokens,
    })
    return {}


def _inject_l1_facts(messages: list[Any], facts: str) -> list[Any]:
    """将 L1 提取的关键事实注入到 system prompt"""
    if not facts:
        return messages

    result = []
    injected = False
    for msg in messages:
        if isinstance(msg, SystemMessage) and not injected:
            # 在第一条 system message 中追加
            content = msg.content + f"\n\n[Compression Context - Key Facts]\n{facts}"
            result.append(SystemMessage(content=content))
            injected = True
        else:
            result.append(msg)

    # 如果没有 system message，创建一个
    if not injected:
        result.insert(0, SystemMessage(content=f"[Compression Context - Key Facts]\n{facts}"))

    return result


def _fire_precompact_hook(session: Any, trigger: str) -> None:
    """触发 PreCompact Hook"""
    if session.hook_runner is None:
        return
    try:
        session.hook_runner.run(
            HookEvent.PreCompact,
            HookPayload(
                event=HookEvent.PreCompact,
                compact_trigger=trigger,
                session_id=session.session_id,
                workspace=str(session.workspace),
            ),
        )
    except Exception as exc:
        logger.debug("[compress] PreCompact hook skipped: %s", exc)


def _get_context_window(session: Any) -> int:
    """获取 context window 大小（从环境变量或默认值）"""
    import os
    try:
        return int(os.environ.get("CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW


def _get_writer() -> Any:
    """获取 langgraph stream writer；无 stream 上下文时返回 no-op"""
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except RuntimeError:
        return lambda event: None
