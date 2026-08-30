"""
dot.coding.extensions.api — ExtensionAPI 扩展注册接口

扩展通过 setup(ext, ctx) 入口函数注册工具、命令、事件监听、hook。
ctx 提供运行上下文（workspace / host / logger / 配置 / 运行时依赖）。

扩展模块可选定义 teardown(ctx) — /reload 或卸载时调用，用于释放连接等资源。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dot.agent.events import AgentEvent
from dot.agent.tools import AgentTool


@dataclass
class ExtensionContext:
    """扩展运行上下文 — setup(ext, ctx) 的第二个参数

    一个扩展一份实例，extension_name / logger 按扩展名区分。
    """
    workspace: Path
    extension_name: str
    host: Any = None  # CodingHost | None（测试环境可能无 host）
    config: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("dot.ext"))

    def get_dep(self, key: str, default: Any = None) -> Any:
        """读取运行时依赖（由 host 注入，如 "host"）"""
        return self._deps.get(key, default) if self._deps else default

    _deps: dict[str, Any] = field(default_factory=dict, repr=False)


class ExtensionAPI(Protocol):
    """扩展注册接口（API 表面的唯一定义处）"""

    def register_tool(self, tool: AgentTool) -> None:
        """注册一个工具"""
        ...

    def register_command(self, name: str, handler: Callable[..., Any]) -> None:
        """注册一个斜杠命令"""
        ...

    def on_event(
        self,
        event_type: str,
        handler: Callable[[AgentEvent], Awaitable[None] | None],
    ) -> None:
        """订阅事件（当前为全量广播，event_type 仅作声明用途）"""
        ...

    def register_tool_call_hook(
        self, name: str, fn: Callable[..., Awaitable[None] | None],
    ) -> None:
        """注册工具执行前 hook：fn(tool_call) -> (blocked, reason) | None

        fail-closed：hook 抛异常视为阻止执行。
        """
        ...

    def register_tool_result_hook(
        self, name: str, fn: Callable[..., Awaitable[None] | None],
    ) -> None:
        """注册工具执行后 hook：fn(tool_call, result, is_error) -> (result, is_error) | None

        fail-closed：hook 抛异常视为错误结果。
        """
        ...

    def add_prompt_section(self, section: str) -> None:
        """向 system prompt 添加段落"""
        ...

    def send_user_message(self, content: str) -> None:
        """发送用户消息到 agent"""
        ...
