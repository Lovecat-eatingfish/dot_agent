"""
dot.host — Agent 入口层

  - agent_host: AgentHost 统一入口（初始化 SessionManager / hosts / hooks）
"""
from __future__ import annotations

from .agent_host import AgentHost

__all__ = ["AgentHost"]
