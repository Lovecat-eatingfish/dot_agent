"""
dot — Coding Agent 三层架构

包结构：
  dot.ai      Provider 抽象层（LLM 调用、流式响应、配置目录）
  dot.agent   Agent 核心（消息循环、工具系统、事件流）
  dot.coding  Coding 应用层（CLI、扩展、MCP、Skills、会话管理）

设计原则：
  - 三层严格单向依赖：coding → agent → ai
  - 零 langchain/langgraph/chromadb 依赖
  - AgentTool frozen dataclass + callable（零继承）
  - 双循环架构：外层 workflow + 内层 agent loop
  - 三层事件系统：ProviderEvent → AgentEvent → CodingEvent
"""
from __future__ import annotations

# Layer 1: Provider 抽象
from .ai import (
    ModelProvider,
    ProviderEvent,
    canonicalize_provider_stream,
    ProviderCatalog,
    estimate_context_tokens,
    OpenAISettings,
)

# Layer 2: Agent 核心
from .agent import (
    AgentTool,
    AgentToolResult,
    AgentEvent,
    AgentHarness,
    run_agent_loop,
    AgentLoopResult,
    execute_tool_safely,
    repair_tool_history,
)

# Layer 3: Coding 应用
from .coding import (
    run_workflow,
    WorkflowPhase,
    WorkflowContext,
    ValidationResult,
    PermissionManager,
    get_permission_manager,
    AgentMode,
    CommandRegistry,
    get_command_registry,
    SlashResult,
    CodingHost,
    CodingEvent,
    SessionState,
    ToolContext,
    ExtensionRuntime,
    ExtensionLoader,
    ExtensionAPI,
    ExtensionGeneration,
)

__all__ = [
    # ai
    "ModelProvider",
    "ProviderEvent",
    "canonicalize_provider_stream",
    "ProviderCatalog",
    "estimate_context_tokens",
    "OpenAISettings",
    # agent
    "AgentTool",
    "AgentToolResult",
    "AgentEvent",
    "AgentHarness",
    "run_agent_loop",
    "AgentLoopResult",
    "execute_tool_safely",
    "repair_tool_history",
    # coding
    "run_workflow",
    "WorkflowPhase",
    "WorkflowContext",
    "ValidationResult",
    "PermissionManager",
    "get_permission_manager",
    "AgentMode",
    "CommandRegistry",
    "get_command_registry",
    "SlashResult",
    "CodingHost",
    "CodingEvent",
    "SessionState",
    "ToolContext",
    "ExtensionRuntime",
    "ExtensionLoader",
    "ExtensionAPI",
    "ExtensionGeneration",
]
