"""
dot.coding.session — 会话管理

职责分离：
  - SessionState: 可序列化的会话状态（session_id / workspace / messages / config）
  - ToolContext: 不可序列化的运行时上下文（cwd / hook_runner / mcp_host / read_files / message_seq）
  - Session: 消息历史 + 文件快照 + 配置（兼容旧接口）
  - SessionManager: 生命周期（create / restore / switch / branch）
  - SessionStorage: 持久化层（append-only JSONL）
  - SessionTree: 分支管理（parent_id 链接）
"""
from __future__ import annotations

from .state import SessionState, ToolContext, HookRunner, McpHost
from .session import Session, SessionConfig, FileSnapshot
from .manager import SessionManager
from .storage import SessionStorage
from .tree import SessionTree

__all__ = [
    "SessionState",
    "ToolContext",
    "HookRunner",
    "McpHost",
    "Session",
    "SessionConfig",
    "FileSnapshot",
    "SessionManager",
    "SessionStorage",
    "SessionTree",
]
