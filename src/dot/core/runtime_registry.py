"""
运行时对象注册表（进程级）

Session 的易变对象（compiled_graph / persistence / mcp_host /
skill_host / hook_runner / runtime）不参与持久化。
SessionManager 初始化 session 时把它们挂进注册表，
恢复 / 扩展场景从这里重新挂载到 Session 上。

进程重启场景：get_or_create 先重建 hosts 并注册，
顺序天然保证注册表可用。
"""
from __future__ import annotations

import threading
from typing import Any

_REGISTRY: dict[str, dict[str, Any]] = {}
_REGISTRY_LOCK = threading.Lock()


def register(session_id: str, **objects: Any) -> None:
    """注册（合并）session 的运行时对象"""
    with _REGISTRY_LOCK:
        _REGISTRY.setdefault(session_id, {}).update(objects)


def get(session_id: str) -> dict[str, Any]:
    """获取 session 注册的全部运行时对象"""
    with _REGISTRY_LOCK:
        return dict(_REGISTRY.get(session_id, {}))


def clear(session_id: str) -> None:
    """清除 session 的注册项"""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(session_id, None)


def attach(session: Any) -> None:
    """把注册表里的运行时对象挂到 Session 上（已挂载的不覆盖）"""
    with _REGISTRY_LOCK:
        objects = _REGISTRY.get(session.session_id)
    if not objects:
        return
    for key, value in objects.items():
        if getattr(session, key, None) is None:
            setattr(session, key, value)
