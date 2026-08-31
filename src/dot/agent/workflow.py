"""Agent-backed workflow node adapters.

This module bridges the generic workflow protocol to the project's
``AgentHarness``. Other model implementations, including an optional
LangChain ``create_agent`` adapter, can live beside this module without
adding their dependency to ``dot.workflow``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass

from dot.ai.types import AssistantMessage
from dot.agent.events import AgentEvent, MessageEndEvent
from dot.agent.harness import AgentHarness
from dot.workflow.context import WorkflowContext

PromptFn = Callable[[WorkflowContext], tuple[str, str | None]]
ResultFn = Callable[[WorkflowContext, str], None]


@dataclass(frozen=True, slots=True)
class AgentNode:
    """Workflow node adapter backed by :class:`AgentHarness`."""

    name: str
    harness: AgentHarness
    prompt_fn: PromptFn
    on_result: ResultFn | None = None

    async def run(self, ctx: WorkflowContext) -> AsyncIterator[AgentEvent]:
        content, system = self.prompt_fn(ctx)
        text_parts: list[str] = []
        failure: str | None = None
        cancel_watcher = asyncio.create_task(
            _cancel_harness_when_requested(ctx, self.harness),
        )
        try:
            async for event in self.harness.prompt(content, system=system):
                yield event
                if isinstance(event, MessageEndEvent):
                    msg = event.message
                    if isinstance(msg, AssistantMessage):
                        if msg.stop_reason in {"error", "aborted"}:
                            failure = msg.error_message or f"agent stopped with {msg.stop_reason}"
                        elif msg.text.strip():
                            text_parts.append(msg.text)
        finally:
            cancel_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watcher

        # Let WorkflowGraph emit its normal cancellation event instead of
        # converting a cooperative agent abort into a node failure.
        if ctx.signal.is_cancelled():
            return

        if failure is not None:
            raise RuntimeError(f"agent node {self.name} failed: {failure}")

        result = "".join(text_parts).strip()
        ctx.set_result(self.name, result)
        if self.on_result is not None:
            self.on_result(ctx, result)


async def _cancel_harness_when_requested(ctx: WorkflowContext, harness: AgentHarness) -> None:
    """Bridge the workflow cancellation token to the harness token."""
    while not ctx.signal.is_cancelled():
        await asyncio.sleep(0.05)
    harness.cancel()


__all__ = ["AgentNode"]
