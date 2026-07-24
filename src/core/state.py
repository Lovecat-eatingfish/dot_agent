from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypedDict, Annotated, Any
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from core.approval import ApprovalRequest, ApprovalDecision


# 文件的快照对象
@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    mtime_ns: int
    complete: bool


# web search 的搜索结果
@dataclass(frozen=True)
class SourceItem(TypedDict, total=False):
    title: str | None
    url: str
    content: str
    score: float


@dataclass
class RuntimeState:
    workspace: Path  # 绝对路径。

    # 审批的模式
    approval_mode: str = "inline"

    # 审批的操作
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None

    source: list[SourceItem] = field(default_factory=list)

    # bash操作的配置项目
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None

    # 文件的快照
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)

    # 作用是安全验证一个路径是否在工作区（workspace）目录内，防止路径遍历攻击或意外访问工作区外的文件。
    def assert_workspace_path(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError(f"path must stay inside workspace: {workspace}")
        return resolved

    # 记录文件的快照
    def record_read(self, path: Path, *, complete: bool) -> None:
        stat = path.stat()
        resolved = path.resolve()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            complete=complete,
        )

    def snapshot_for(self, path):
        return self.read_files.get(path.resolve())


# 每一项的todo plan计划
class TodoItem(TypedDict):
    id: str
    content: str
    status: str
    note: str


# 校验节点的结果， 使用命令校验的结果
class VerificationResult(TypedDict):
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str


# agent 的交接流程
class AgentHandoff(TypedDict, total=False):
    from_agent: str
    to_agent: str
    instruction: str
    result: str


# 检查节点结果
class VerificationCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class CompressionEvent(TypedDict, total=False):
    before_tokens: int
    after_tokens: int
    removed_messages: int
    summary: str
    next_node: str


class LayeredMemory(TypedDict, total=False):
    rules: dict[str, Any]
    working_memory: dict[str, Any]
    history_summary_store: dict[str, Any]


class DotAgentGraphState(TypedDict, total=False):
    task: str
    runtime: RuntimeState

    # 上下文信息
    session_context: str
    messages: Annotated[list[BaseMessage], add_messages]  # 完整的对话消息列表，包含用户消息、AI 回复、工具调用记录等

    # 意图信息
    intent_route: str  # chat | plan
    intent_reason: str  # 意图的原因
    intent_confidence: float  # 置信度
    chat_response: str  # 聊天返回地结果

    # 计划信息：
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    verification_results: list[VerificationResult]
    step_count: int  # 当前任务执行的累计步骤数（跨多轮对话） final_node 中 +1，跨轮次传递

    # 验证的结果
    passed: bool
    attempts: int  # 重试的次数
    max_attempts: int

    # 意图选择的信息
    intent_route: str
    intent_reason: str
    intent_confidence: float

    # 意图选择的是 聊天
    chat_response: str

    # 重试的次数
    attempts: int
    max_attempts: int
    iteration: int  # 当前轮次内的循环次数（planner → monitor → verifier 循环）
    max_iterations: int

    # 搜索agent的相关信息
    research_notes: str
    sources: list[SourceItem]

    agent_handoffs: list[AgentHandoff]
    code_agent_summary: str
    verifier_summary: str
    verification_checks: list[VerificationCheck]

    # 会话的信息
    session_id: str
    session_turn: int
    session_context: str
    last_actor_summary: str

    # 上下文压缩相关的
    context_tokens: int #  当前消息列表消耗的 token 数量估算值
    context_monitor_counter: int # 上下文监控计数器，每经过一个节点 +1
    is_context_overflow: bool # 上下文是否溢出（token 超过 context_threshold）
    context_summary: str
    context_token_count: int
    context_token_limit: int
    context_should_compress: bool
    context_next_node: str
    compression_events: list[CompressionEvent]
    memory_snapshot: LayeredMemory
    history_summary: str
    last_error: str
    metadata: dict[str, Any]
