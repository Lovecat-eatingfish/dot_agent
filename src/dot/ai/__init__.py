"""
dot.ai — Provider 抽象层（底层）

职责：
  - LLM 调用和流式响应处理
  - Provider 协议定义
  - 流式响应格式统一
  - Provider 配置目录（catalog.toml）
  - 上下文窗口估算

依赖：仅 httpx + pydantic（零框架依赖）
"""
from __future__ import annotations

from .provider import ModelProvider, ProviderCancellationToken
from .events import ProviderEvent
from .stream import canonicalize_provider_stream
from .catalog import ProviderCatalog, ProviderConfig, ModelConfig
from .config import OpenAISettings
from .limits import estimate_context_tokens, ContextWindowInfo

__all__ = [
    # provider
    "ModelProvider",
    "ProviderCancellationToken",
    # events
    "ProviderEvent",
    # stream
    "canonicalize_provider_stream",
    # catalog
    "ProviderCatalog",
    "ProviderConfig",
    "ModelConfig",
    # config
    "OpenAISettings",
    # limits
    "estimate_context_tokens",
    "ContextWindowInfo",
]
