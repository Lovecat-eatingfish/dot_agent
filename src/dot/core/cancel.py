"""
dot.core.cancel — CancellationToken Protocol

只读 Protocol，只暴露 is_cancelled()。
取消能力保留在 Harness 内部，形成清晰的权限边界。
Provider 和 Tool 各有独立定义，可独立演化。

放在 core 层：ai / agent / workflow 都需要引用令牌类型，
而它们互为兄弟层，不能相互 import。
"""
from __future__ import annotations

from typing import Protocol


class ProviderCancellationToken(Protocol):
    """Provider 层取消令牌（只读）"""

    def is_cancelled(self) -> bool:
        """当前流是否应停止"""
        ...


class ToolCancellationToken(Protocol):
    """Tool 层取消令牌（只读）"""

    def is_cancelled(self) -> bool:
        """工具执行是否应停止"""
        ...


class SimpleCancellationToken:
    """简单的可写取消令牌（Harness 内部使用）"""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled
