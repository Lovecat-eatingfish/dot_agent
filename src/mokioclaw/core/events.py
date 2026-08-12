"""
事件总线核心

提供可订阅的事件系统，替代单回调 writer 模式。
支持按事件类型订阅、过滤、优先级、装饰器注册。

使用方式：
    bus = EventBus()
    bus.subscribe("tool_call", my_handler)
    bus.emit({"type": "tool_call", "name": "BashTool"})
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class EventBus:
    """线程安全的事件总线

    属性：
        _subscribers: {event_type: [(priority, handler), ...]}
        _global_handlers: [(priority, handler)] — 接收所有事件
        _lock: 写入锁（emit 时保护）
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[tuple[int, Callable[[dict[str, Any]], None]]]] = {}
        self._global_handlers: list[tuple[int, Callable[[dict[str, Any]], None]]] = []
        self._lock = threading.Lock()
        self._enabled = True

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], None],
        *,
        priority: int = 0,
    ) -> Callable[[], None]:
        """订阅指定类型的事件

        Args:
            event_type: 事件类型字符串，如 "tool_call"、"checkpoint_saved"
            handler: 事件处理函数，接收 event dict
            priority: 优先级（数字越大越先执行，默认 0）

        Returns:
            unsubscribe 函数，调用后取消订阅
        """
        with self._lock:
            handlers = self._subscribers.setdefault(event_type, [])
            handlers.append((priority, handler))
            handlers.sort(key=lambda x: -x[0])

        def unsubscribe() -> None:
            with self._lock:
                h = self._subscribers.get(event_type, [])
                self._subscribers[event_type] = [(p, fn) for p, fn in h if fn is not handler]

        return unsubscribe

    def subscribe_all(
        self,
        handler: Callable[[dict[str, Any]], None],
        *,
        priority: int = 0,
    ) -> Callable[[], None]:
        """订阅所有事件（全局处理器）

        Args:
            handler: 事件处理函数
            priority: 优先级

        Returns:
            unsubscribe 函数
        """
        with self._lock:
            self._global_handlers.append((priority, handler))
            self._global_handlers.sort(key=lambda x: -x[0])

        def unsubscribe() -> None:
            with self._lock:
                self._global_handlers = [(p, fn) for p, fn in self._global_handlers if fn is not handler]

        return unsubscribe

    def on(self, event_type: str, *, priority: int = 0) -> Callable:
        """装饰器：订阅指定类型的事件

        Usage:
            @bus.on("tool_call")
            def handle_tool_call(event):
                print(event["name"])
        """
        def decorator(handler: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
            self.subscribe(event_type, handler, priority=priority)
            return handler
        return decorator

    def emit(self, event: dict[str, Any]) -> None:
        """发出事件，同步调用所有匹配的处理器

        Args:
            event: 事件字典，必须包含 "type" 字段
        """
        if not self._enabled:
            return

        event_type = event.get("type", "")
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))
            globals_list = list(self._global_handlers)

        for _, handler in handlers:
            try:
                handler(event)
            except Exception:
                pass  # 一个 handler 失败不影响其他

        for _, handler in globals_list:
            try:
                handler(event)
            except Exception:
                pass

    def disable(self) -> None:
        """禁用事件总线（emit 变为 no-op）"""
        self._enabled = False

    def enable(self) -> None:
        """启用事件总线"""
        self._enabled = True

    def clear(self) -> None:
        """清除所有订阅（测试用）"""
        with self._lock:
            self._subscribers.clear()
            self._global_handlers.clear()


# 模块级单例（进程内共享）
_default_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """获取默认事件总线单例"""
    global _default_bus
    if _default_bus is None:
        with _bus_lock:
            if _default_bus is None:
                _default_bus = EventBus()
    return _default_bus


def reset_event_bus() -> None:
    """重置默认事件总线（测试用）"""
    global _default_bus
    with _bus_lock:
        _default_bus = None
