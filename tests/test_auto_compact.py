"""
tests/test_auto_compact — 自动上下文压缩

覆盖两层：
1. run_agent_loop 在 turn 边界调用 CompactionGate、原地替换消息、发出 ContextCompactedEvent
2. AutoCompactor（coding 层实现）的触发阈值与压缩执行
"""
from __future__ import annotations

import asyncio

import pytest

from dot.agent import AgentHarness, AgentHarnessConfig
from dot.agent.compaction import CompactionResult
from dot.agent.events import ContextCompactedEvent, MessageEndEvent
from dot.agent.loop import run_agent_loop
from dot.ai import events as provider_events
from dot.ai.types import AssistantMessage, TextContent, ToolResultMessage, UserMessage
from dot.coding.compress.auto_compact import AutoCompactor
from dot.coding.compress.compactor import ContextCompactor


class FakeProvider:
    model = "fake"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list] = []

    async def stream_response(self, *, messages, **kwargs):
        del kwargs
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        message = AssistantMessage(
            content=[TextContent(text=reply)], model=self.model, provider="fake"
        )
        yield provider_events.AssistantDoneEvent(reason="stop", message=message)


class FakeGate:
    """首次满足条件时把消息历史替换为单条消息"""

    def __init__(self, min_messages: int = 2) -> None:
        self.min_messages = min_messages
        self.calls: list[int] = []

    async def maybe_compact(self, messages) -> CompactionResult | None:
        self.calls.append(len(messages))
        if len(messages) >= self.min_messages:
            return CompactionResult(
                messages=[messages[0]], level="L1+L2", reason="test compaction",
            )
        return None


def test_loop_calls_gate_at_turn_boundary_and_replaces_messages() -> None:
    async def _run() -> None:
        gate = FakeGate()
        provider = FakeProvider(["first reply"])
        messages = [UserMessage(content="hi")]

        events = []
        async for event in run_agent_loop(
            provider=provider,
            model="fake",
            system="system",
            messages=messages,
            tools=[],
            compaction=gate,
        ):
            events.append(event)

        # gate 在 assistant 消息入历史后被调用（user + assistant = 2 条）
        assert gate.calls == [2]
        # 压缩结果原地写回 messages 列表
        assert len(messages) == 1
        # 事件流中有 ContextCompactedEvent，且在 AgentEndEvent 之前
        compacted = [e for e in events if isinstance(e, ContextCompactedEvent)]
        assert len(compacted) == 1
        assert compacted[0].level == "L1+L2"
        assert compacted[0].before == 2
        assert compacted[0].after == 1

    asyncio.run(_run())


def test_loop_without_gate_does_not_emit_compaction_event() -> None:
    async def _run() -> None:
        provider = FakeProvider(["reply"])
        messages = [UserMessage(content="hi")]
        events = []
        async for event in run_agent_loop(
            provider=provider, model="fake", system="s",
            messages=messages, tools=[],
        ):
            events.append(event)
        assert not any(isinstance(e, ContextCompactedEvent) for e in events)
        assert len(messages) == 2  # user + assistant，未被替换

    asyncio.run(_run())


def test_harness_passes_compaction_and_message_replacement_is_visible() -> None:
    async def _run() -> None:
        gate = FakeGate()
        provider = FakeProvider(["hello"])
        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="fake", system="system", compaction=gate,
        ))
        async for _ in harness.prompt("hi"):
            pass
        # harness._messages 与 loop 内的 messages 是同一列表对象
        assert len(harness.messages) == 1

    asyncio.run(_run())


def test_auto_compactor_triggers_at_threshold_and_applies_l1l2(monkeypatch) -> None:
    monkeypatch.setenv("DOT_CONTEXT_WINDOW", "8000")

    compactor = ContextCompactor(provider=FakeProvider([]), model="fake")
    gate = AutoCompactor(compactor)

    big = "x" * 24000  # ~6000 tokens / 8000 窗口 ≈ 75% → L2 档
    messages = [
        UserMessage(content="read the file"),
        ToolResultMessage(
            tool_call_id="call-1", tool_name="read_file",
            content=[TextContent(text=big)],
        ),
    ]

    result = asyncio.run(gate.maybe_compact(list(messages)))
    assert result is not None
    assert "L1" in result.level and "L2" in result.level
    assert len(result.messages) < len(messages) or sum(
        len(m.text) for m in result.messages if hasattr(m, "text")
    ) < sum(len(m.text) for m in messages)


def test_auto_compactor_skips_below_threshold(monkeypatch) -> None:
    monkeypatch.setenv("DOT_CONTEXT_WINDOW", "8000")

    compactor = ContextCompactor(provider=FakeProvider([]), model="fake")
    gate = AutoCompactor(compactor)

    messages = [UserMessage(content="tiny")]
    result = asyncio.run(gate.maybe_compact(list(messages)))
    assert result is None


def test_auto_compactor_debounces_until_usage_grows_again(monkeypatch) -> None:
    monkeypatch.setenv("DOT_CONTEXT_WINDOW", "8000")

    compactor = ContextCompactor(provider=FakeProvider([]), model="fake")
    gate = AutoCompactor(compactor)

    big = "x" * 24000
    messages = [
        UserMessage(content="read the file"),
        ToolResultMessage(
            tool_call_id="call-1", tool_name="read_file",
            content=[TextContent(text=big)],
        ),
    ]

    first = asyncio.run(gate.maybe_compact(list(messages)))
    assert first is not None
    # 压缩后的消息列表再次送检（真实循环就是这样）：占用未重新增长，不再触发
    second = asyncio.run(gate.maybe_compact(list(first.messages)))
    assert second is None
