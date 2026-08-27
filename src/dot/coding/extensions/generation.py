"""
dot.coding.extensions.generation — ExtensionGeneration liveness token

每次 /reload 创建新 generation，旧引用的所有操作立即抛出 ExtensionError。
防止热重载后的幽灵注册。
"""
from __future__ import annotations


class ExtensionError(Exception):
    """扩展操作错误（generation 过期等）"""
    pass


class ExtensionGeneration:
    """扩展生命周期令牌

    每次 /reload 创建新 generation，旧引用的操作立即抛错。
    """

    def __init__(self) -> None:
        self._alive = True
        self._id = id(self)

    @property
    def is_alive(self) -> bool:
        return self._alive

    def invalidate(self) -> None:
        """使当前 generation 失效"""
        self._alive = False

    def ensure_alive(self) -> None:
        """检查 generation 是否存活，否则抛出异常"""
        if not self._alive:
            raise ExtensionError(
                f"Extension generation {self._id} has been invalidated. "
                "Use the latest generation after /reload."
            )
