"""
LangGraph 工作流状态定义

本模块定义了 MokioClaw 多智能体工作流的状态结构。

状态流转：
1. 用户输入 → intent_router（意图识别）
2. intent_router → planner（任务规划）或 chat_responder（轻量聊天）
3. planner → search_agent/code_agent（委派任务）
4. context_monitor → verifier（校验结果）
5. verifier → planner（失败重试）或 final（结束）

设计原则：
- state 只存驱动控制流的核心字段
- 其余信息（plan_summary 细节、verification_checks、sources 等）从 messages[] 中重建
- PersistedState 仅存跨轮恢复的最简状态
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from mokioclaw.state.runtime import RuntimeState


class TodoItem(TypedDict):
    id: str
    content: str
    status: str
    note: str


class VerificationCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class PersistedState(TypedDict, total=False):
    """跨轮持久化状态：从 MokioGraphState 映射，仅含恢复所需的最简字段

    其余信息（plan_summary、todos、verification_checks、sources 等）
    从 session-{id}.json 的 messages[] 中重建。
    """
    task: str
    passed: bool | None
    attempts: int
    repair_instruction: str
    last_error: str
    final_answer: str


class MokioGraphState(TypedDict, total=False):
    """LangGraph 工作流的全局状态

    只保留驱动控制流的核心字段（17 个）。其余通过 messages[] 或常量推导。
    """
    # ========== 任务核心 ==========
    task: str
    runtime: RuntimeState

    # ========== 消息与输出 ==========
    messages: Annotated[list[BaseMessage], add_messages]
    final_answer: str
    last_error: str

    # ========== 意图路由 ==========
    intent_route: str
    intent_reason: str
    intent_confidence: float
    chat_response: str

    # ========== 验证循环 ==========
    passed: bool
    attempts: int
    max_attempts: int
    repair_instruction: str

    # ========== 计划与待办 ==========
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]
