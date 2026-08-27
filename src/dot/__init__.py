"""
dot — 新一代 Coding Agent 核心模块（独立实现，不依赖 mokioclaw）

包结构：
  core/     基础设施（日志 / LLM / hooks / 审批 / 路径安全 / 预算 / 运行时）
  tools/    基础工具（file / bash / glob / grep）+ 渐进披露元工具
  mcp/      MCP 协议栈 + 渐进披露
  skills/   Skill 发现与渐进披露
  session/  会话域（Session / 持久化 / SessionManager，自定义介入与回滚）
  graph/    LangGraph 图编排 + 节点提示词

设计原则（doc/architecture-design.md）：
  - Session IS State：节点通过 state["session"] 直接读写，无独立 state dict
  - 消息手动管理：session.messages.append() / session.messages = compressed_list
  - 单会话 SessionManager：get_or_create 三级优先级
  - 自定义人工介入：不依赖 langgraph interrupt/checkpoint，靠 session.json 持久化状态
"""
from __future__ import annotations

from .compress import CompressionState, context_compress_node
from .core import (
    HookEvent,
    HookPayload,
    HookResult,
    HookRunner,
    create_model,
    execute_tool_by_name,
)
from .graph import DotAgentState, build_graph, compile_graph
from .graph.prompts import (
    get_coding_system_prompt,
    get_plan_system_prompt,
    get_valid_system_prompt,
)
from .session import Session, SessionManager, SessionPersistence, persist_turn, TurnState, AgentContext
from .tools.meta import build_tools_for_session
from dot.host.agent_host import AgentHost
from .trace import Tracer, get_tracer, init_tracer

__all__ = [
    # compress
    "CompressionState",
    "context_compress_node",
    # session
    "Session",
    "TurnState",
    "AgentContext",
    "SessionManager",
    "SessionPersistence",
    "persist_turn",
    # graph
    "DotAgentState",
    "build_graph",
    "compile_graph",
    "get_plan_system_prompt",
    "get_coding_system_prompt",
    "get_valid_system_prompt",
    # core
    "HookEvent",
    "HookPayload",
    "HookResult",
    "HookRunner",
    "create_model",
    "execute_tool_by_name",
    # tools
    "build_tools_for_session",
    "AgentHost",
    # trace
    "Tracer",
    "get_tracer",
    "init_tracer",
]
