"""
dot.agent — Agent 核心层（中层）

职责：
  - Agent Loop（LLM 调用 → 工具执行 → 循环）
  - 工具系统（AgentTool frozen dataclass）
  - 事件流（AgentEvent discriminated union）
  - 消息历史管理（AgentHarness）
  - 工具调用兜底（execute_tool_safely）
  - 历史自愈（repair_tool_history）

依赖：dot.ai + pydantic（零外部框架依赖）
"""
from __future__ import annotations

from .tools import AgentTool, AgentToolResult
from .events import AgentEvent
from .harness import AgentHarness
from .loop import run_agent_loop
from .types import AgentLoopResult, AgentLoopConfig, TokenUsage
from .cancel import ProviderCancellationToken, ToolCancellationToken
from .executor import execute_tool_safely
from .history import repair_tool_history

__all__ = [
    # tools
    "AgentTool",
    "AgentToolResult",
    # events
    "AgentEvent",
    # harness
    "AgentHarness",
    # loop
    "run_agent_loop",
    # types
    "AgentLoopResult",
    "AgentLoopConfig",
    "TokenUsage",
    # cancel
    "ProviderCancellationToken",
    "ToolCancellationToken",
    # executor
    "execute_tool_safely",
    # history
    "repair_tool_history",
]
