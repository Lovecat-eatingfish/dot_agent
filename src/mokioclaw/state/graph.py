"""
LangGraph 工作流状态定义

本模块定义了 MokioClaw 多智能体工作流的状态结构。

状态流转：
1. 用户输入 → intent_router（意图识别）
2. intent_router → planner（任务规划）或 chat_responder（轻量聊天）
3. planner → search_agent/code_agent（委派任务）
4. context_monitor → verifier（校验结果）
5. verifier → planner（失败重试）或 final（结束）

状态分类：
- 任务状态：task, plan_summary, todos, acceptance_criteria
- 验证状态：passed, attempts, verification_checks
- 意图状态：intent_route, intent_confidence
- 上下文状态：context_summary, context_token_count
- 会话状态：session_id, session_turn
- 智能体交互：agent_handoffs, code_agent_summary
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from mokioclaw.state.runtime import RuntimeState


class TodoItem(TypedDict):
    """待办任务项

    由 planner 创建，code_agent 更新状态，verifier 校验完成情况。

    属性：
        id: 任务唯一标识符，如 "todo-1"
        content: 任务描述，如 "创建 index.html"
        status: 当前状态 - pending/in_progress/completed/blocked
        note: 备注信息，如失败原因或阻塞说明
    """
    id: str
    content: str
    status: str
    note: str


class VerificationResult(TypedDict):
    """单条命令的执行结果

    记录 verifier 执行验证命令的详细结果，用于判断任务是否完成。

    属性：
        command: 执行的命令字符串
        ok: 命令是否成功（exit_code == 0）
        exit_code: 进程退出码，None 表示超时或未执行
        stdout: 标准输出内容（可能被截断）
        stderr: 标准错误输出内容（可能被截断）
    """
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str


class SourceItem(TypedDict, total=False):
    """搜索来源条目

    由 search_agent 搜集，用于在最终输出中引用信息来源。

    属性：
        title: 来源标题
        url: 来源 URL
        content: 摘要内容（可选）
        score: 相关性评分（可选）
    """
    title: str
    url: str
    content: str
    score: float


class AgentHandoff(TypedDict, total=False):
    """智能体交接记录

    记录 planner 与专业智能体之间的任务委派和结果返回。

    属性：
        from_agent: 发起方智能体名称，如 "planner"
        to_agent: 接收方智能体名称，如 "codeAgent" 或 "searchAgent"
        instruction: 委派的具体指令
        result: 执行结果摘要
    """
    from_agent: str
    to_agent: str
    instruction: str
    result: str


class VerificationCheck(TypedDict, total=False):
    """单条校验项结果

    由 verifier 返回的结构化校验结果，用于展示详细的通过/失败情况。

    属性：
        name: 校验项名称，如 "file_exists" 或 "content_check"
        passed: 是否通过
        detail: 详细说明，失败时包含原因
    """
    name: str
    passed: bool
    detail: str


class CompressionEvent(TypedDict, total=False):
    """上下文压缩事件记录

    当消息列表过长时，系统会自动压缩上下文。此记录保存压缩的详细信息。

    属性：
        before_tokens: 压缩前的 token 数量
        after_tokens: 压缩后的 token 数量
        removed_messages: 被移除的消息数量
        summary: 压缩后的摘要内容
        next_node: 压缩后继续执行的节点名称
        strategy: 使用的压缩策略（hard / incremental / step_triggered）
    """
    before_tokens: int
    after_tokens: int
    removed_messages: int
    summary: str
    next_node: str
    strategy: str


class LayeredMemory(TypedDict, total=False):
    """分层记忆结构

    实现了三层记忆机制，帮助智能体在长对话中保持上下文：
    1. rules: 持久化规则（跨任务保持）
    2. working_memory: 工作记忆（当前任务的关键信息）
    3. history_summary_store: 历史摘要（过往对话的压缩总结）

    属性：
        rules: 持久化规则配置
        working_memory: 当前任务的工作记忆
        history_summary_store: 历史对话摘要存储
    """
    rules: dict[str, Any]
    working_memory: dict[str, Any]
    history_summary_store: dict[str, Any]


class MokioGraphState(TypedDict, total=False):
    """LangGraph 工作流的全局状态

    这是整个多智能体系统的核心状态，所有节点（planner、verifier 等）
    都通过读写此状态来协作完成任务。

    状态分组说明：
    ┌─────────────────────────────────────────────────────────┐
    │ 任务核心                                                │
    │   task: 用户输入的原始任务                               │
    │   runtime: 运行时配置（工作区、审批模式等）              │
    │   plan_summary: 当前计划摘要                            │
    │   todos: 待办任务列表                                   │
    │   acceptance_criteria: 验收标准                         │
    │   verification_commands: 需要执行的验证命令              │
    ├─────────────────────────────────────────────────────────┤
    │ 验证循环                                                │
    │   passed: 任务是否通过验证                              │
    │   attempts: 当前尝试次数                                │
    │   max_attempts: 最大尝试次数                            │
    │   verification_results: 验证命令的执行结果              │
    │   verification_checks: 校验项的详细结果                 │
    ├─────────────────────────────────────────────────────────┤
    │ 意图路由                                                │
    │   intent_route: 路由决策 - "chat" 或 "workflow"         │
    │   intent_reason: 路由决策的原因                         │
    │   intent_confidence: 路由置信度（0-1）                  │
    │   chat_response: 聊天模式的回复内容                     │
    ├─────────────────────────────────────────────────────────┤
    │ 上下文管理                                              │
    │   context_summary: 当前上下文摘要                       │
    │   context_token_count: 当前 token 数量                  │
    │   context_token_limit: token 数量上限                   │
    │   context_should_compress: 是否需要压缩                 │
    │   context_next_node: 压缩后跳转的节点                   │
    │   compression_events: 历次压缩记录                      │
    │   memory_snapshot: 分层记忆快照                         │
    │   history_summary: 历史对话摘要                         │
    ├─────────────────────────────────────────────────────────┤
    │ 智能体交互                                              │
    │   agent_handoffs: 任务委派记录列表                      │
    │   code_agent_summary: 代码智能体的执行摘要              │
    │   search_agent_summary: 搜索智能体的执行摘要            │
    │   verifier_summary: 校验器的校验摘要                    │
    │   last_actor_summary: 最后执行的智能体摘要              │
    │   research_notes: 搜索智能体收集的研究笔记              │
    │   sources: 搜索来源列表                                 │
    ├─────────────────────────────────────────────────────────┤
    │ 会话管理                                                │
    │   session_id: 会话唯一标识                              │
    │   session_turn: 当前会话轮次                            │
    │   session_context: 会话上下文信息                       │
    ├─────────────────────────────────────────────────────────┤
    │ 消息与输出                                              │
    │   messages: 完整的消息列表（自动合并）                  │
    │   final_answer: 最终输出结果                            │
    │   last_error: 最近一次错误信息                          │
    │   metadata: 额外的元数据                                │
    └─────────────────────────────────────────────────────────┘
    """
    # ========== 任务核心 ==========
    task: str
    runtime: RuntimeState
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]

    # ========== 验证循环 ==========
    verification_results: list[VerificationResult]
    passed: bool
    attempts: int
    max_attempts: int
    verification_checks: list[VerificationCheck]

    # ========== 意图路由 ==========
    intent_route: str
    intent_reason: str
    intent_confidence: float
    chat_response: str

    # ========== 上下文管理 ==========
    context_summary: str
    context_token_count: int
    context_token_limit: int
    context_should_compress: bool
    context_next_node: str
    context_compression_strategy: str  # "hard" | "soft" | "step_triggered" | "none"
    compression_events: list[CompressionEvent]
    memory_snapshot: LayeredMemory
    history_summary: str

    # ========== 智能体交互 ==========
    agent_handoffs: list[AgentHandoff]
    code_agent_summary: str
    search_agent_summary: str
    verifier_summary: str
    last_actor_summary: str
    research_notes: str
    sources: list[SourceItem]

    # ========== 路由决策 ==========
    planner_route: str
    planner_route_instruction: str
    repair_instruction: str

    # ========== 会话管理 ==========
    session_id: str
    session_turn: int
    session_context: str

    # ========== 消息与输出 ==========
    messages: Annotated[list[BaseMessage], add_messages]
    final_answer: str
    last_error: str
    metadata: dict[str, Any]
