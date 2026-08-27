"""
dot.coding.extensions.api — ExtensionAPI 扩展注册接口

扩展通过 setup(ext: ExtensionAPI) 入口函数注册工具、命令、事件监听。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from dot.agent.events import AgentEvent
from dot.agent.tools import AgentTool


class ExtensionAPI(Protocol):
    """扩展注册接口"""

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
        """订阅事件"""
        ...

    def add_prompt_section(self, section: str) -> None:
        """向 system prompt 添加段落"""
        ...

    def send_user_message(self, content: str) -> None:
        """发送用户消息到 agent"""
        ...
