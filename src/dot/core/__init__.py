"""
dot.core — 基础设施层

  - log / llm:          日志与 LLM 工厂
  - hooks / hook_loader: Hook 执行引擎与配置加载
  - path_security:      路径白名单/黑名单/防穿越
  - tool_result_budget: 工具大输出落盘预算
  - git_utils:          git init/commit/reset
  - utils:              共享工具函数 + execute_tool_by_name
"""
from __future__ import annotations

from .log import get_logger, setup_logging
from .llm import create_model
from .hooks import HookEvent, HookPayload, HookResult, HookRunner
from .hook_loader import load_hooks_into_runner
from .utils import execute_tool_by_name, last_ai_content

__all__ = [
    "get_logger",
    "setup_logging",
    "create_model",
    "HookEvent",
    "HookPayload",
    "HookResult",
    "HookRunner",
    "load_hooks_into_runner",
    "execute_tool_by_name",
    "last_ai_content",
]
