"""
dot.coding.compress.compactor — ContextCompactor（上下文压缩逻辑）

纯压缩逻辑：上下文占用估算、L1/L2 同步压缩、L3 摘要。
不持有 harness / session 状态——写回与持久化由调用方（CodingHost）完成。

- compact()      ：L1/L2 同步，L3 异步调度（/compact 手动路径，不阻塞 REPL）
- compact_async():L1/L2/L3 全部内联 await（agent 循环 turn 边界自动压缩用，
                   保证压缩结果在本轮内生效，不与运行中的回合并发）
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dot.ai.limits import ContextWindowInfo, estimate_context_tokens
from dot.ai.types import AssistantMessage

from .l1_extract import compact_l1
from .l2_summarize import compact_l2
from .l3_semantic import compact_l3
from .planner import CompactionLevel, CompactionPlan, plan_compaction

if TYPE_CHECKING:
    from dot.ai.providers import OpenAIProvider
    from dot.ai.types import AgentMessage

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 128000


@dataclass
class CompactionOutcome:
    """一次压缩的执行结果"""
    messages: list["AgentMessage"]           # 压缩后的消息列表（未压缩时为原列表）
    applied: list[str] = field(default_factory=list)  # 已同步执行的级别，如 ["L1", "L2"]
    scheduled_l3: bool = False               # L3 是否已调度/执行
    report: str = ""                         # 给人看的报告文本

    @property
    def level(self) -> str:
        """已应用级别的紧凑表示，如 "L1+L2" """
        return "+".join(a.split("(")[0] for a in self.applied)


class ContextCompactor:
    """三级上下文压缩器

    L1（≥50%）：去掉可恢复的 tool 结果；L2（≥70%）：删除老旧 tool 调用；
    L3（≥85%）：LLM 摘要替换旧消息。
    """

    def __init__(self, *, provider: "OpenAIProvider", model: str) -> None:
        self._provider = provider
        self._model = model

    # ============================================================
    # 占用估算
    # ============================================================

    @staticmethod
    def estimate(messages: Sequence["AgentMessage"]) -> ContextWindowInfo:
        """估算当前上下文占用

        锚定策略：取最近一条带 usage 的 assistant 消息作为权威基值
        （其后的消息为增量估算）；窗口大小可用环境变量 DOT_CONTEXT_WINDOW
        覆盖（默认 128000）。
        """
        import os

        provider_tokens = 0
        after_index = 0
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if isinstance(msg, AssistantMessage) and msg.usage.total_tokens > 0:
                provider_tokens = msg.usage.total_tokens
                after_index = i + 1
                break
        try:
            context_window = int(os.environ.get("DOT_CONTEXT_WINDOW", str(DEFAULT_CONTEXT_WINDOW)))
        except ValueError:
            context_window = DEFAULT_CONTEXT_WINDOW
        return estimate_context_tokens(
            list(messages),
            provider_tokens=provider_tokens,
            context_window=context_window,
            after_index=after_index,
        )

    # ============================================================
    # 压缩执行
    # ============================================================

    async def compact_async(self, messages: list["AgentMessage"]) -> CompactionOutcome:
        """全内联压缩：L1/L2/L3 依次 await，返回最终结果

        供 agent 循环在 turn 边界调用——压缩完成后才继续下一轮 LLM 调用。
        """
        plan = plan_compaction(self.estimate(messages))
        if plan.level is CompactionLevel.NONE:
            return CompactionOutcome(
                messages=messages,
                report=f"no compaction needed ({plan.reason})",
            )

        compacted, applied = self._apply_l1_l2(messages, plan)

        if plan.level is CompactionLevel.L3:
            compacted = await compact_l3(
                list(compacted), provider=self._provider, model=self._model,
            )
            applied.append("L3")

        return CompactionOutcome(
            messages=compacted,
            applied=applied,
            scheduled_l3=plan.level is CompactionLevel.L3,
            report=f"compacted [{'+'.join(applied)}]: "
                   f"{len(messages)} -> {len(compacted)} messages ({plan.reason})",
        )

    def compact(self, messages: list["AgentMessage"]) -> CompactionOutcome:
        """应用压缩：L1/L2 同步执行，L3 仅调度；返回结果与报告文本"""
        plan = plan_compaction(self.estimate(messages))
        if plan.level is CompactionLevel.NONE:
            return CompactionOutcome(
                messages=messages,
                report=f"no compaction needed ({plan.reason})",
            )

        compacted, applied = self._apply_l1_l2(messages, plan)

        scheduled_l3 = False
        if plan.level is CompactionLevel.L3:
            scheduled_l3 = self.schedule_l3(compacted)
            applied.append("L3(async scheduled)" if scheduled_l3 else "L3(skipped)")

        return CompactionOutcome(
            messages=compacted,
            applied=applied,
            scheduled_l3=scheduled_l3,
            report=f"compacted [{'+'.join(applied)}]: "
                   f"{len(messages)} -> {len(compacted)} messages ({plan.reason})",
        )

    # ============================================================
    # 内部机制
    # ============================================================

    @staticmethod
    def _apply_l1_l2(
        messages: list["AgentMessage"],
        plan: CompactionPlan,
    ) -> tuple[list["AgentMessage"], list[str]]:
        """按计划执行 L1/L2（无 LLM 调用），返回 (压缩后消息, 已应用级别)"""
        compacted = messages
        applied: list[str] = []
        if plan.level in (CompactionLevel.L1, CompactionLevel.L2, CompactionLevel.L3):
            compacted = compact_l1(compacted)
            applied.append("L1")
        if plan.level in (CompactionLevel.L2, CompactionLevel.L3):
            compacted = compact_l2(compacted)
            applied.append("L2")
        return compacted, applied

    def schedule_l3(self, messages: list["AgentMessage"], on_done: Callable[[list], None] | None = None) -> bool:
        """在运行中的事件循环上调度 LLM 摘要压缩，完成后回调 on_done

        返回是否成功调度（无事件循环时跳过并告警）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[compactor] L3 compaction skipped: no running event loop")
            return False

        async def _run() -> None:
            compacted = await compact_l3(list(messages), provider=self._provider, model=self._model)
            if on_done is not None:
                on_done(compacted)
            logger.info("[compactor] L3 semantic compaction done (%d messages)", len(compacted))

        loop.create_task(_run())
        return True
