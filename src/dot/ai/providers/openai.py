"""
dot.ai.providers.openai — OpenAI Provider 实现

使用 httpx 直接调用 OpenAI API，不依赖 langchain。
将 OpenAI SSE 格式解析为统一的 ProviderEvent 流。
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

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


class OpenAIProvider:
    """OpenAI API Provider

    实现 ModelProvider Protocol，将 OpenAI SSE 格式转换为 ProviderEvent 流。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[ToolCall],
        signal: object | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """流式调用 OpenAI API，产出 ProviderEvent"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = self._build_body(model, system, messages, tools)

        assistant = AssistantMessage(model=model, provider="openai")
        started = False
        content_index = 0
        current_text = ""
        current_thinking = ""
        current_tool_calls: dict[int, dict] = {}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if signal and hasattr(signal, "is_cancelled") and signal.is_cancelled():
                        yield AssistantErrorEvent(reason="aborted", error=assistant)
                        return

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")

                    if not started:
                        started = True
                        yield AssistantStartEvent(partial=assistant)

                    # Text content
                    if "content" in delta and delta["content"] is not None:
                        current_text += delta["content"]
                        assistant.content = self._build_content(current_text, current_thinking, current_tool_calls)
                        yield TextDeltaEvent(
                            content_index=content_index,
                            delta=delta["content"],
                            partial=assistant,
                        )

                    # Thinking content (for models that support it)
                    if "thinking" in delta and delta["thinking"] is not None:
                        current_thinking += delta["thinking"]
                        assistant.content = self._build_content(current_text, current_thinking, current_tool_calls)
                        yield ThinkingDeltaEvent(
                            content_index=content_index,
                            delta=delta["thinking"],
                            partial=assistant,
                        )

                    # Tool calls
                    if "tool_calls" in delta:
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                                yield ToolCallStartEvent(
                                    content_index=content_index + len(current_tool_calls),
                                    partial=assistant,
                                )

                            tc = current_tool_calls[idx]
                            if "id" in tc_delta:
                                tc["id"] = tc_delta["id"]
                            if "function" in tc_delta:
                                func = tc_delta["function"]
                                if "name" in func:
                                    tc["name"] = func["name"]
                                if "arguments" in func:
                                    tc["arguments"] += func["arguments"]

                            assistant.content = self._build_content(current_text, current_thinking, current_tool_calls)
                            yield ToolCallDeltaEvent(
                                content_index=content_index + len(current_tool_calls),
                                delta=tc_delta.get("function", {}).get("arguments", ""),
                                partial=assistant,
                            )

                    # Usage
                    usage_data = data.get("usage")
                    if usage_data:
                        assistant.usage = Usage(
                            input=usage_data.get("prompt_tokens", 0),
                            output=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                        )

                    # Finish
                    if finish_reason:
                        if current_text:
                            yield TextEndEvent(
                                content_index=content_index,
                                content=current_text,
                                partial=assistant,
                            )
                            content_index += 1
                        if current_thinking:
                            yield ThinkingEndEvent(
                                content_index=content_index,
                                content=current_thinking,
                                partial=assistant,
                            )
                            content_index += 1
                        for idx in sorted(current_tool_calls.keys()):
                            tc = current_tool_calls[idx]
                            tool_call = ToolCall(
                                id=tc["id"],
                                name=tc["name"],
                                arguments=self._parse_arguments(tc["arguments"]),
                            )
                            assistant.content = self._build_content(current_text, current_thinking, current_tool_calls)
                            yield ToolCallEndEvent(
                                content_index=content_index + idx,
                                tool_call=tool_call,
                                partial=assistant,
                            )

                        reason = "stop"
                        if finish_reason == "length":
                            reason = "length"
                        elif finish_reason == "tool_calls":
                            reason = "toolUse"
                        assistant.stop_reason = reason
                        assistant.content = self._build_content(current_text, current_thinking, current_tool_calls)
                        yield AssistantDoneEvent(reason=reason, message=assistant)
                        return

        # Stream ended without [DONE]
        if started:
            yield AssistantDoneEvent(reason="stop", message=assistant)

    def _build_body(
        self,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[ToolCall],
    ) -> dict:
        """构建请求体"""
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": [{"role": "system", "content": system}],
        }

        for msg in messages:
            if hasattr(msg, "role"):
                if msg.role == "user":
                    body["messages"].append({
                        "role": "user",
                        "content": msg.text if hasattr(msg, "text") else str(msg.content),
                    })
                elif msg.role == "assistant":
                    entry: dict[str, Any] = {"role": "assistant"}
                    if hasattr(msg, "text") and msg.text:
                        entry["content"] = msg.text
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in msg.tool_calls
                        ]
                    body["messages"].append(entry)
                elif msg.role == "toolResult":
                    body["messages"].append({
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.text if hasattr(msg, "text") else str(msg.content),
                    })

        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "description": "",
                        "parameters": tc.arguments,
                    },
                }
                for tc in tools
            ]

        return body

    def _build_content(
        self,
        text: str,
        thinking: str,
        tool_calls: dict[int, dict],
    ) -> list:
        """构建内容块列表"""
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
                arguments=self._parse_arguments(tc["arguments"]),
            ))
        return content

    @staticmethod
    def _parse_arguments(args_str: str) -> dict:
        """解析工具调用参数"""
        if not args_str:
            return {}
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            return {}
