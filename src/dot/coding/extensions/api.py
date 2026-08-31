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
from typing import Any, Literal, Protocol

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

    def register_hook(
        self,
        timing: str,
        fn: Callable[..., Any] | None = None,
        *,
        name: str = "",
        matcher: str = ".*",
    ) -> Any:
        """在指定时机注册 hook（支持装饰器），matcher 为正则匹配规则

        时机（HOOK_TIMINGS）：
          before_tool_call   fn(tool_call) -> (blocked, reason) | None   可阻止，fail-closed
          after_tool_call    fn(tool_call, result, is_error) -> (result, is_error) | None
          agent_start / agent_end / turn_start / turn_end /
          message_start / message_end /
          tool_execution_start / tool_execution_update / tool_execution_end
                             fn(event)                                    观察点
        matcher 正则：工具时机匹配工具名（如 r"bash"），生命周期时机匹配时机名。
        """
        ...

    def on_event(
        self,
        event_type: str,
        handler: Callable[[AgentEvent], Awaitable[None] | None],
    ) -> None:
        """被动监听事件（不做匹配、不可阻止；需要拦截/改写请用 register_hook）"""
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

    def send_user_message(
        self,
        content: str,
        *,
        deliver_as: Literal["steer", "follow_up"] = "follow_up",
    ) -> None:
        """发送用户消息到 agent；运行中默认作为 follow-up 排队。"""
        ...

    def queue_steering_message(self, content: str) -> None:
        """把消息排队到当前 agent 回合的 steering 队列。"""
        ...

    def queue_follow_up_message(self, content: str) -> None:
        """把消息排队到当前 agent 回合结束后的 follow-up 队列。"""
        ...
