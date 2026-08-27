"""
dot.ai.providers — 具体 Provider 实现

每个 Provider 实现 ModelProvider Protocol，
负责将自身的 SSE 格式解析为统一的 ProviderEvent 流。
"""
from __future__ import annotations
