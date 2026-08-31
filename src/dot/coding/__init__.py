"""
dot.coding — Coding 应用层（顶层）

职责：
  - 内置 coding 工作流（plan → code → validate，基于 dot.workflow 引擎）
  - CLI 入口
  - 会话管理（Session / SessionManager / SessionStorage）
  - 扩展系统（Extension API / Loader / Runtime）
  - MCP 集成（内置扩展）
  - Skills（提示词注入）
  - 三级上下文压缩
  - 斜杠命令系统
  - 权限管控
  - 事件驱动链路追踪

依赖：dot.workflow + dot.agent + typer + rich + mcp + prompt_toolkit
"""
from __future__ import annotations

from .workflow import (
    build_coding_workflow,
    create_context,
    get_state,
    HumanInterventionHandler,
    HumanInterventionMode,
    parse_verdict,
    run_workflow,
)
from .state import CodingWorkflowState, ValidationResult
from .permission import PermissionManager, get_permission_manager
from .modes import AgentMode
from .commands import CommandRegistry, get_command_registry, SlashResult
from .host import CodingHost
from .events import CodingEvent
from .extensions import ExtensionRuntime, ExtensionLoader, ExtensionAPI, ExtensionGeneration

__all__ = [
    # workflow（coding 业务实例）
    "run_workflow",
    "build_coding_workflow",
    "create_context",
    "get_state",
    "HumanInterventionHandler",
    "HumanInterventionMode",
    "parse_verdict",
    "CodingWorkflowState",
    "ValidationResult",
    # permission
    "PermissionManager",
    "get_permission_manager",
    # modes
    "AgentMode",
    # commands
    "CommandRegistry",
    "get_command_registry",
    "SlashResult",
    # host
    "CodingHost",
    # events
    "CodingEvent",
    # extensions
    "ExtensionRuntime",
    "ExtensionLoader",
    "ExtensionAPI",
    "ExtensionGeneration",
]
