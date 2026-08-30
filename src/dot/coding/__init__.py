"""
dot.coding — Coding 应用层（顶层）

职责：
  - 外层 Workflow 循环（plan → code → validate → human_intervene）
  - CLI 入口
  - 会话管理（Session / SessionManager / SessionStorage）
  - 扩展系统（Extension API / Loader / Runtime）
  - MCP 集成（内置扩展）
  - Skills（提示词注入）
  - 三级上下文压缩
  - 斜杠命令系统
  - 权限管控
  - 事件驱动链路追踪

依赖：dot.agent + typer + rich + mcp + prompt_toolkit
"""
from __future__ import annotations

from .workflow import run_workflow
from .state import WorkflowPhase, WorkflowContext, ValidationResult
from .permission import PermissionManager, get_permission_manager
from .modes import AgentMode
from .commands import CommandRegistry, get_command_registry, SlashResult
from .host import CodingHost
from .events import CodingEvent
from .extensions import ExtensionRuntime, ExtensionLoader, ExtensionAPI, ExtensionGeneration

__all__ = [
    # workflow
    "run_workflow",
    "WorkflowPhase",
    "WorkflowContext",
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
    # session
    # extensions
    "ExtensionRuntime",
    "ExtensionLoader",
    "ExtensionAPI",
    "ExtensionGeneration",
]
