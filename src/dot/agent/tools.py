"""
dot.agent.tools — AgentTool frozen dataclass + AgentToolResult

轻量工具系统，零继承。工具通过 execute_fn callable 注册。
frozen=True 防止注册后被篡改；slots=True 减少内存占用。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import model_validator

from dot.ai.types import ImageContent, TextContent, WireModel

JSONValue = str | int | float | bool | None | dict | list


# ============================================================
# AgentToolResult（先定义，供后续类型引用）
# ============================================================

class AgentToolResult(WireModel):
    """工具执行结果"""
    content: list[TextContent | ImageContent] = []
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    terminate: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextContent))


# ============================================================
# Protocols & Type Aliases
# ============================================================

class ToolCallRenderer(Protocol):
    def __call__(self, arguments: Mapping[str, JSONValue]) -> str | None: ...


class ToolResultRenderer(Protocol):
    def __call__(self, result: AgentToolResult, *, expanded: bool) -> str | None: ...


ToolUpdateCallback = Callable[[AgentToolResult], None]


class ToolExecutor(Protocol):
    def __call__(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]:
        """执行一个工具调用"""
        ...


ToolExecutionMode = Literal["sequential", "parallel"]
ToolArgumentPreparer = Callable[[object], Mapping[str, JSONValue]]


# ============================================================
# AgentTool
# ============================================================

@dataclass(frozen=True, slots=True)
class AgentTool:
    """暴露给 Agent Loop 的工具定义"""
    name: str
    label: str
    description: str
    parameters: Mapping[str, JSONValue]
    execute_fn: ToolExecutor
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    prepare_arguments: ToolArgumentPreparer | None = None
    execution_mode: ToolExecutionMode = "parallel"
    render_call: ToolCallRenderer | None = None
    render_result: ToolResultRenderer | None = None

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        return self.parameters

    async def execute(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        return await self.execute_fn(tool_call_id, arguments, signal, on_update)
