"""
dot.agent.cancel — CancellationToken Protocol（兼容转发）

实现已移至 dot.core.cancel（core 层，供 ai/agent/workflow 共同依赖）。
此模块保留 re-export，避免破坏既有 `from dot.agent.cancel import ...` 引用。
"""
from dot.core.cancel import (
    ProviderCancellationToken,
    SimpleCancellationToken,
    ToolCancellationToken,
)

__all__ = ["ProviderCancellationToken", "SimpleCancellationToken", "ToolCancellationToken"]
