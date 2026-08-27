"""
dot.ai.provider — ModelProvider Protocol + CancellationToken

ModelProvider 是 LLM Provider 的统一接口，只有一个方法 stream_response。
CancellationToken 是只读 Protocol，只暴露 is_cancelled()，取消能力保留在调用方。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .events import ProviderEvent
from .types import AgentMessage, ToolCall


class ProviderCancellationToken(Protocol):
    """Provider 层取消令牌（只读）"""

    def is_cancelled(self) -> bool:
        """当前流是否应停止"""
        ...


class ModelProvider(Protocol):
    """Provider 中立的模型流式响应接口

    每个 Provider（OpenAI / Anthropic / 自定义）实现此 Protocol，
    负责将自身的 SSE 格式解析为统一的 ProviderEvent 流。
    """

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[ToolCall],
        signal: ProviderCancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """流式返回一个 assistant 响应

        Args:
            model: 模型标识
            system: 系统提示词
            messages: 消息历史
            tools: 可用工具列表
            signal: 取消令牌
            session_id: 会话 ID（用于请求路由或 prompt-cache 亲和性）

        Yields:
            ProviderEvent: 流式事件（text_delta / tool_call_start / done 等）
        """
        ...
