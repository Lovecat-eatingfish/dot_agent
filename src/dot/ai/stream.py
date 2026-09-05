"""
dot.ai.stream — 流式响应统一

canonicalize_provider_stream 桥接函数：将不同 Provider 的 SSE 格式
统一为 ProviderEvent 流。支持增量迁移——旧 parser 和新事件格式共存。

归一化状态与各事件类型的处理逻辑在 _StreamNormalizer 中按单一职责拆分，
主循环只做分发与终止判断。
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


class _StreamNormalizer:
    """单个响应流的归一化状态机

    持有累积中的 AssistantMessage 与内容块游标，
    每种原始事件类型对应一个 _on_* 处理方法（返回 0 或 1 个 ProviderEvent）。
    """

    def __init__(self, *, model: str = "unknown", provider: str = "unknown") -> None:
        self.message = AssistantMessage(model=model, provider=provider)
        self.content_index = 0
        self.started = False
        self.text_started = False
        self.thinking_started = False

    def handle(self, raw: dict) -> ProviderEvent | None:
        """处理一个原始事件；返回对应 ProviderEvent（无需产出时为 None）"""
        event_type = raw.get("type", "")
        handler = getattr(self, f"_on_{event_type}", None)
        return handler(raw) if handler is not None else None

    # ---- 生命周期 ----

    def _on_start(self, raw: dict) -> ProviderEvent | None:
        if self.started:
            return None
        self.started = True
        return AssistantStartEvent(partial=self.message)

    def _on_done(self, raw: dict) -> AssistantDoneEvent:
        reason = raw.get("reason", "stop")
        self.message.stop_reason = reason
        return AssistantDoneEvent(reason=reason, message=self.message)

    def _on_error(self, raw: dict) -> AssistantErrorEvent:
        reason = raw.get("reason", "error")
        self.message.error_message = raw.get("error", "Unknown error")
        return AssistantErrorEvent(reason=reason, error=self.message)

    # ---- 文本块 ----

    def _on_text_start(self, raw: dict) -> ProviderEvent | None:
        if self.text_started:
            return None
        self.text_started = True
        return TextStartEvent(content_index=self.content_index, partial=self.message)

    def _on_text_delta(self, raw: dict) -> ProviderEvent | None:
        delta = raw.get("delta", "")
        if not delta:
            return None
        self.message.content.append(TextContent(text=delta))
        return TextDeltaEvent(
            content_index=self.content_index,
            delta=delta,
            partial=self.message,
        )

    def _on_text_end(self, raw: dict) -> TextEndEvent:
        text_content = raw.get("content", "")
        self.content_index += 1
        self.text_started = False
        return TextEndEvent(
            content_index=self.content_index - 1,
            content=text_content,
            partial=self.message,
        )

    # ---- 思考块 ----

    def _on_thinking_start(self, raw: dict) -> ProviderEvent | None:
        if self.thinking_started:
            return None
        self.thinking_started = True
        return ThinkingStartEvent(content_index=self.content_index, partial=self.message)

    def _on_thinking_delta(self, raw: dict) -> ProviderEvent | None:
        delta = raw.get("delta", "")
        if not delta:
            return None
        self.message.content.append(ThinkingContent(thinking=delta))
        return ThinkingDeltaEvent(
            content_index=self.content_index,
            delta=delta,
            partial=self.message,
        )

    def _on_thinking_end(self, raw: dict) -> ThinkingEndEvent:
        thinking_content = raw.get("content", "")
        self.content_index += 1
        self.thinking_started = False
        return ThinkingEndEvent(
            content_index=self.content_index - 1,
            content=thinking_content,
            partial=self.message,
        )

    # ---- 工具调用块 ----

    def _on_toolcall_start(self, raw: dict) -> ToolCallStartEvent:
        return ToolCallStartEvent(content_index=self.content_index, partial=self.message)

    def _on_toolcall_delta(self, raw: dict) -> ToolCallDeltaEvent:
        delta = raw.get("delta", "")
        return ToolCallDeltaEvent(
            content_index=self.content_index,
            delta=delta,
            partial=self.message,
        )

    def _on_toolcall_end(self, raw: dict) -> ToolCallEndEvent:
        tool_call = ToolCall(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            arguments=raw.get("arguments", {}),
        )
        self.message.content.append(tool_call)
        index = self.content_index
        self.content_index += 1
        return ToolCallEndEvent(
            content_index=index,
            tool_call=tool_call,
            partial=self.message,
        )


# 终止型事件：处理后立即结束流
_TERMINAL_EVENTS = (AssistantDoneEvent, AssistantErrorEvent)


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
    normalizer = _StreamNormalizer(model=model, provider=provider)

    async for raw in raw_events:
        event = normalizer.handle(raw)
        if event is not None:
            yield event
        if isinstance(event, _TERMINAL_EVENTS):
            return

    # 流意外结束
    if normalizer.started:
        yield AssistantDoneEvent(reason="stop", message=normalizer.message)
