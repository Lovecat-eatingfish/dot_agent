"""
dot.ai.providers.openai — OpenAI Provider 实现

使用 openai SDK（v2）直接调用，不再手动写 httpx SSE 解析。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from dot.ai.config import OpenAISettings
from dot.core.cancel import SimpleCancellationToken
from dot.ai.events import (
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
from dot.ai.types import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)

if TYPE_CHECKING:
    from dot.agent import AgentTool


class OpenAIProvider:
    """OpenAI API Provider — 基于 openai SDK v2"""

    def __init__(
            self,
            api_key: str | None = None,
            base_url: str | None = None,
            model: str | None = None,
            timeout: float | None = None,
    ) -> None:
        overrides: dict[str, Any] = {}
        if api_key is not None:
            overrides["api_key"] = api_key
        if base_url is not None:
            overrides["base_url"] = base_url
        if model is not None:
            overrides["model"] = model
        if timeout is not None:
            overrides["timeout"] = timeout

        # todo： 暂时先使用env的配置
        self._settings = OpenAISettings(**overrides)
        self._client = AsyncOpenAI(
            api_key=self._settings.api_key,
            base_url=self._settings.base_url,
            timeout=self._settings.timeout,
        )

    @property
    def model(self) -> str:
        """Default model from settings"""
        return self._settings.model

    def _require_key(self) -> str:
        return self._settings.resolve_api_key()

    from dot.agent import AgentTool

    async def stream_response(

            self,
            *,
            model: str,
            system: str,
            messages: list[AgentMessage],
            tools: list[AgentTool],
            signal: SimpleCancellationToken | None = None,
            session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """流式调用 OpenAI API，产出 ProviderEvent"""
        api_key = self._require_key()

        openai_messages = _build_openai_messages(system, messages)
        openai_tools = _build_openai_tools(tools) if tools else None

        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=openai_messages,
                tools=openai_tools,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as exc:
            yield AssistantErrorEvent(reason="error", error=AssistantMessage(
                model=model, provider="openai", error_message=str(exc),
            ))
            return

        assistant = AssistantMessage(model=model, provider="openai")
        started = False
        content_index = 0
        current_text = ""
        current_thinking = ""
        tool_calls_buf: dict[int, dict] = {}

        async for chunk in stream:
            # 检查取消信号
            if signal and hasattr(signal, "is_cancelled") and signal.is_cancelled():
                yield AssistantErrorEvent(reason="aborted", error=assistant)
                return

            if not started:
                started = True
                yield AssistantStartEvent(partial=assistant)

            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                # usage-only chunk
                continue

            delta = choice.delta

            # Text
            if delta.content:
                current_text += delta.content
                assistant.content = _build_content(current_text, current_thinking, tool_calls_buf)
                yield TextDeltaEvent(
                    content_index=content_index,
                    delta=delta.content,
                    partial=assistant,
                )

            # Thinking (reasoning_content for reasoning models)
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                current_thinking += delta.reasoning_content
                assistant.content = _build_content(current_text, current_thinking, tool_calls_buf)
                yield ThinkingDeltaEvent(
                    content_index=content_index,
                    delta=delta.reasoning_content,
                    partial=assistant,
                )

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_buf:
                        tool_calls_buf[idx] = {"id": "", "name": "", "arguments": ""}
                        yield ToolCallStartEvent(
                            content_index=content_index + len(tool_calls_buf),
                            partial=assistant,
                        )
                    tc = tool_calls_buf[idx]
                    if tc_delta.id:
                        tc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc["arguments"] += tc_delta.function.arguments
                    yield ToolCallDeltaEvent(
                        content_index=content_index + len(tool_calls_buf),
                        delta=(tc_delta.function.arguments or "") if tc_delta.function else "",
                        partial=assistant,
                    )

            # Finish
            if choice.finish_reason:
                if current_text:
                    yield TextEndEvent(content_index=content_index, content=current_text, partial=assistant)
                    content_index += 1
                if current_thinking:
                    yield ThinkingEndEvent(content_index=content_index, content=current_thinking, partial=assistant)
                    content_index += 1
                for idx in sorted(tool_calls_buf.keys()):
                    tc = tool_calls_buf[idx]
                    tool_call = ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=_parse_arguments(tc["arguments"]),
                    )
                    yield ToolCallEndEvent(
                        content_index=content_index + idx,
                        tool_call=tool_call,
                        partial=assistant,
                    )

                reason = _map_finish_reason(choice.finish_reason)
                assistant.stop_reason = reason
                assistant.content = _build_content(current_text, current_thinking, tool_calls_buf)
                if chunk.usage:
                    assistant.usage = Usage(
                        input=chunk.usage.prompt_tokens or 0,
                        output=chunk.usage.completion_tokens or 0,
                        total_tokens=chunk.usage.total_tokens or 0,
                    )
                yield AssistantDoneEvent(reason=reason, message=assistant)
                return

        # Stream ended without finish_reason
        if started:
            yield AssistantDoneEvent(reason="stop", message=assistant)


# ============================================================
# 转换函数
# ============================================================

def _build_openai_messages(system: str, messages: list[AgentMessage]) -> list[dict]:
    """将 AgentMessage 列表转为 openai SDK 格式"""
    result = [{"role": "system", "content": system}]
    for msg in messages:
        if msg.role == "user":
            result.append({"role": "user", "content": msg.text})
        elif msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.text:
                entry["content"] = msg.text
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": _serialize_args(tc.arguments)},
                    }
                    for tc in msg.tool_calls
                ]
            result.append(entry)
        elif msg.role == "toolResult":
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.text,
            })
    return result


def _build_openai_tools(tools: list[AgentTool]) -> list[dict]:
    """将 AgentTool 列表转为 openai SDK 工具格式"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or tool.label,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _build_content(text: str, thinking: str, tool_calls: dict[int, dict]) -> list:
    content = []
    if thinking:
        content.append(ThinkingContent(thinking=thinking))
    if text:
        content.append(TextContent(text=text))
    for idx in sorted(tool_calls.keys()):
        tc = tool_calls[idx]
        content.append(ToolCall(
            id=tc["id"],
            name=tc["name"],
            arguments=_parse_arguments(tc["arguments"]),
        ))
    return content


def _parse_arguments(args_str: str) -> dict:
    if not args_str:
        return {}
    try:
        return __import__("json").loads(args_str)
    except Exception:
        return {}


def _serialize_args(args: dict) -> str:
    try:
        return __import__("json").dumps(args)
    except Exception:
        return "{}"


def _map_finish_reason(reason: str) -> str:
    return {"stop": "stop", "length": "length", "tool_calls": "toolUse"}.get(reason, "stop")
