from __future__ import annotations

import asyncio

from dot.agent import AgentHarness, AgentHarnessConfig
from dot.agent.events import MessageEndEvent
from dot.ai import events as provider_events
from dot.ai.types import AssistantMessage, TextContent, UserMessage


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


def test_harness_queues_are_injected_into_the_same_run() -> None:
    asyncio.run(_test_harness_queues_are_injected_into_the_same_run())


async def _test_harness_queues_are_injected_into_the_same_run() -> None:
    provider = FakeProvider(["first", "after steer", "after follow-up"])
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="fake", system="system")
    )

    async for event in harness.prompt("start"):
        if isinstance(event, MessageEndEvent):
            message = event.message
            if isinstance(message, AssistantMessage) and message.text == "first":
                harness.steer("correct this")
                harness.follow_up("then summarize")

    users = [message.text for message in harness.messages if isinstance(message, UserMessage)]
    assert users == ["start", "correct this", "then summarize"]
    assert len(provider.calls) == 3
    assert [message.text for message in provider.calls[1] if isinstance(message, UserMessage)] == [
        "start", "correct this"
    ]
    assert [message.text for message in provider.calls[2] if isinstance(message, UserMessage)] == [
        "start", "correct this", "then summarize"
    ]


def test_harness_can_edit_latest_queued_message() -> None:
    provider = FakeProvider([])
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="fake", system="system")
    )
    harness.steer("first")
    harness.steer("latest")
    harness.follow_up("later")

    assert harness.pop_latest_follow_up().text == "later"
    assert harness.pop_latest_steering().text == "latest"
    assert [message.text for message in harness.queued_messages.steering] == ["first"]
