"""
dot — Coding Agent 多层架构

包结构：
  dot.ai       Provider 抽象层（LLM 调用、流式响应、配置目录）
  dot.agent    Agent 核心（消息循环、工具系统、事件流）
  dot.workflow 通用工作流引擎（节点编排、条件路由、生命周期事件）
  dot.coding   Coding 应用层（CLI、扩展、MCP、Skills、会话管理）

设计原则：
  - 层间严格单向依赖：coding → workflow，coding → agent → ai
  - workflow 核心不依赖 langgraph/langchain；LLM 可通过适配器接入
  - AgentTool frozen dataclass + callable（零继承）
  - 通用引擎 + 业务实例：plan→code→validate 只是 dot.workflow 的一个用户
  - 事件系统：ProviderEvent → AgentEvent → WorkflowEvent / CodingEvent
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
    AgentHarnessConfig,
    QueuedMessages,
    AgentNode,
    run_agent_loop,
    execute_tool_safely,
    repair_tool_history,
)

# Layer 3: 通用工作流引擎
from .workflow import (
    END,
    FunctionNode,
    WorkflowNode,
    WorkflowGraph,
    WorkflowContext,
    WorkflowStatus,
    WorkflowCancellationToken,
    SimpleWorkflowCancellationToken,
    WorkflowEvent,
    WorkflowNodeStartEvent,
    WorkflowNodeEndEvent,
    WorkflowErrorEvent,
    WorkflowInterruptEvent,
    WorkflowDoneEvent,
    WorkflowInteractionHandler,
    run_with_interaction,
)

# Layer 4: Coding 应用
from .coding import (
    run_workflow,
    build_coding_workflow,
    create_context,
    get_state,
    CodingWorkflowState,
    HumanInterventionHandler,
    HumanInterventionMode,
    ValidationResult,
    PermissionManager,
    get_permission_manager,
    AgentMode,
    CommandRegistry,
    get_command_registry,
    SlashResult,
    CodingHost,
    CodingEvent,
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
    "AgentHarnessConfig",
    "QueuedMessages",
    "run_agent_loop",
    "execute_tool_safely",
    "repair_tool_history",
    # workflow 引擎
    "END",
    "WorkflowNode",
    "AgentNode",
    "FunctionNode",
    "WorkflowGraph",
    "WorkflowContext",
    "WorkflowStatus",
    "WorkflowCancellationToken",
    "SimpleWorkflowCancellationToken",
    "WorkflowEvent",
    "WorkflowNodeStartEvent",
    "WorkflowNodeEndEvent",
    "WorkflowErrorEvent",
    "WorkflowInterruptEvent",
    "WorkflowDoneEvent",
    "WorkflowInteractionHandler",
    "run_with_interaction",
    # coding
    "run_workflow",
    "build_coding_workflow",
    "create_context",
    "get_state",
    "CodingWorkflowState",
    "HumanInterventionHandler",
    "HumanInterventionMode",
    "ValidationResult",
    "PermissionManager",
    "get_permission_manager",
    "AgentMode",
    "CommandRegistry",
    "get_command_registry",
    "SlashResult",
    "CodingHost",
    "CodingEvent",
    "ExtensionRuntime",
    "ExtensionLoader",
    "ExtensionAPI",
    "ExtensionGeneration",
]
