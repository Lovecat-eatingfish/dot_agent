"""
dot.agent.executor — 工具调用统一兜底层

execute_tool_safely 覆盖所有失败场景，返回 LLM 可操作的错误信息。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping

from dot.ai.types import TextContent

from .tools import AgentTool, AgentToolResult

DEFAULT_TIMEOUT = 30  # 秒


async def execute_tool_safely(
    tool: AgentTool,
    tool_call_id: str,
    arguments: Mapping[str, object],
    *,
    signal: ToolCancellationToken | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    on_update: object | None = None,
) -> tuple[AgentToolResult, bool]:
    """安全执行工具调用，统一处理所有失败场景

    Args:
        tool: 工具定义
        tool_call_id: 工具调用 ID
        arguments: 工具参数
        signal: 取消令牌
        timeout: 超时秒数（默认 30s）
        on_update: 进度回调

    Returns:
        (result, is_error) 元组
    """
    # 1. 取消检查
    if signal is not None and signal.is_cancelled():
        return _error_result("Operation aborted"), True

    # 2. 参数准备
    try:
        if tool.prepare_arguments is not None:
            args = tool.prepare_arguments(arguments)
        else:
            args = arguments
    except Exception as exc:
        return _error_result(f"Invalid arguments: {type(exc).__name__}: {exc}"), True

    # 3. 超时 + 执行
    try:
        result = await asyncio.wait_for(
            tool.execute(tool_call_id, args, signal, on_update),
            timeout=timeout,
        )
        return result, False
    except asyncio.TimeoutError:
        return _error_result(f"Tool execution timed out after {timeout}s"), True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _error_result(f"{type(exc).__name__}: {exc}"), True


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})
