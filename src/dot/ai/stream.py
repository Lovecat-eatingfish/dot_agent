"""
dot.ai.stream — 流式响应统一

canonicalize_provider_stream 桥接函数：将不同 Provider 的 SSE 格式
统一为 ProviderEvent 流。支持增量迁移——旧 parser 和新事件格式共存。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from .events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantStartEvent,
    ProviderEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from .types import AssistantMessage, TextContent, ThinkingContent, ToolCall


async def canonicalize_provider_stream(
    raw_events: AsyncIterator[dict],
    *,
    model: str = "unknown",
    provider: str = "unknown",
) -> AsyncIterator[ProviderEvent]:
    """将原始 SSE 事件流转换为统一的 ProviderEvent 流

    这是一个示例实现，展示如何将字典形式的原始事件转换为类型化的 ProviderEvent。
    具体 Provider 实现应直接产出 ProviderEvent，无需此桥接。

    Args:
        raw_events: 原始事件流（字典形式）
        model: 模型标识
        provider: Provider 标识

    Yields:
        ProviderEvent: 统一的流式事件
    """
    message = AssistantMessage(model=model, provider=provider)
    content_index = 0
    started = False
    text_started = False
    thinking_started = False

    async for raw in raw_events:
        event_type = raw.get("type", "")

        if event_type == "start":
            if not started:
                started = True
                yield AssistantStartEvent(partial=message)

        elif event_type == "text_start":
            if not text_started:
                text_started = True
                yield TextStartEvent(content_index=content_index, partial=message)

        elif event_type == "text_delta":
            delta = raw.get("delta", "")
            if delta:
                message.content.append(TextContent(text=delta))
                yield TextDeltaEvent(
                    content_index=content_index,
                    delta=delta,
                    partial=message,
                )

        elif event_type == "text_end":
            text_content = raw.get("content", "")
            yield TextEndEvent(
                content_index=content_index,
                content=text_content,
                partial=message,
            )
            content_index += 1
            text_started = False

        elif event_type == "thinking_start":
            if not thinking_started:
                thinking_started = True
                yield ThinkingStartEvent(content_index=content_index, partial=message)

        elif event_type == "thinking_delta":
            delta = raw.get("delta", "")
            if delta:
                message.content.append(ThinkingContent(thinking=delta))
                yield ThinkingDeltaEvent(
                    content_index=content_index,
                    delta=delta,
                    partial=message,
                )

        elif event_type == "thinking_end":
            thinking_content = raw.get("content", "")
            yield ThinkingEndEvent(
                content_index=content_index,
                content=thinking_content,
                partial=message,
            )
            content_index += 1
            thinking_started = False

        elif event_type == "toolcall_start":
            yield ToolCallStartEvent(content_index=content_index, partial=message)

        elif event_type == "toolcall_delta":
            delta = raw.get("delta", "")
            yield ToolCallDeltaEvent(
                content_index=content_index,
                delta=delta,
                partial=message,
            )

        elif event_type == "toolcall_end":
            tool_call = ToolCall(
                id=raw.get("id", ""),
                name=raw.get("name", ""),
                arguments=raw.get("arguments", {}),
            )
            message.content.append(tool_call)
            yield ToolCallEndEvent(
                content_index=content_index,
                tool_call=tool_call,
                partial=message,
            )
            content_index += 1

        elif event_type == "done":
            reason = raw.get("reason", "stop")
            message.stop_reason = reason
            yield AssistantDoneEvent(reason=reason, message=message)
            return

        elif event_type == "error":
            reason = raw.get("reason", "error")
            message.error_message = raw.get("error", "Unknown error")
            yield AssistantErrorEvent(reason=reason, error=message)
            return

    # 流意外结束
    if started:
        yield AssistantDoneEvent(reason="stop", message=message)
