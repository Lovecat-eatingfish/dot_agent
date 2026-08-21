"""
运行时状态管理模块

本模块定义了 MokioClaw Agent 的运行时状态，包括：
- 文件快照：追踪已读取文件的状态，防止并发修改冲突
- 运行时配置：工作区路径、审批模式、超时设置等

设计原则：
- RuntimeState 是整个 Agent 执行过程中的核心状态容器
- 所有工具调用都依赖此状态来确定工作区位置和安全边界
- 文件快照机制确保"先读后写"的安全模式
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import BaseMessage
from mokioclaw.core.hooks import HookRunner
from mokioclaw.core.tool_result_budget import ToolResultBudget
from mokioclaw.security.agent_mode import normalize_agent_mode
from mokioclaw.security.approval import ApprovalDecision, ApprovalRequest, normalize_approval_mode
from mokioclaw.reliability.checkpoint import normalize_checkpoint_mode
from mokioclaw.security.path_security import PathAccessDeniedError, PathTraversalError, validate_path_access
from mokioclaw.reliability.trace import normalize_trace_mode


@dataclass(frozen=True)
class FileSnapshot:
    """文件快照，记录文件的修改时间和内容哈希

    用途：
    - 在写入或编辑文件前，检查文件是否已被修改（防止覆盖他人的修改）
    - 追踪文件是否被完整读取（用于决定是否允许写入）
    - 通过 content_hash 做精确的内容变更检测（mtime 粒度不够时）

    属性：
        path: 文件的绝对路径
        mtime_ns: 文件最后修改时间（纳秒级时间戳），用于快速检测文件变更
        content_hash: 文件内容的 sha256 哈希，用于精确检测内容变更
        complete: 文件是否被完整读取（从头到尾），部分读取时不允许写入
    """
    path: Path
    mtime_ns: int
    content_hash: str
    complete: bool


@dataclass
class RuntimeState:
    """Agent 运行时状态，贯穿整个任务执行生命周期

    这是整个系统的核心状态对象，所有工具和节点都依赖它来：
    1. 确定工作区位置（workspace）
    2. 执行安全检查（路径必须在工作区内）
    3. 管理文件读写权限（通过 FileSnapshot）
    4. 控制命令执行行为（超时、输出大小等）
    5. 处理人工审批流程（高风险命令）

    典型使用场景：
    - BashTool 执行命令时，需要 workspace 确定 cwd
    - FileReadTool/WriteTool 需要 workspace 做路径安全检查
    - 高风险命令（如 rm -rf）需要 approval_handler 进行人工确认
    """
    # 工作区根目录，所有文件操作都必须在此目录内
    workspace: Path

    # 已读取文件的快照表，key 是文件绝对路径
    # 用于在写入前检查文件是否被修改过
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)

    # 审批模式：inline（命令行询问）、auto（自动批准）、deny（自动拒绝）
    approval_mode: str = "inline"

    # Agent 运行模式：auto / plan / approve / edit
    agent_mode: str = "auto"

    # 工具权限规则：支持精确名称与通配符（如 mcp__*）
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    # 审批处理函数，当 approval_mode=inline 时，由 CLI 或 TUI 提供
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None

    # 文件写操作消息游标：path → last mutating tool_result message_id（微压缩用）
    file_state_map: dict[str, str] = field(default_factory=dict)

    # 强制下一轮走上下文压缩
    force_compact: bool = False

    # 递增消息序号（用于 file_state_map）；并行工具调用时需加锁
    message_seq: int = 0
    _message_seq_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # MCP 渐进披露：已按需加载的工具名 → StructuredTool
    loaded_mcp_tools: dict[str, Any] = field(default_factory=dict)

    # ToolSearch：已加载的延迟工具（含 MCP / WebSearch / Agent 等）
    loaded_tools: dict[str, Any] = field(default_factory=dict)
    deferred_tool_catalog: str = ""

    # contextModifier：Bash cd 等可更新后续工具看到的工作目录（相对 workspace）
    cwd: Path | None = None

    # BashTool 默认超时时间（秒），单次命令执行的最大等待时间
    bash_default_timeout_seconds: int = 120

    # BashTool 最大允许超时（秒），用户设置的值不能超过此限制
    bash_max_timeout_seconds: int = 600

    # BashTool 输出截断阈值（字符数），超出部分会保存到文件
    bash_max_output_chars: int = 6000

    # 额外的环境变量文件路径，会在执行 Bash 命令时加载
    bash_env_file: Path | None = None

    # 检查点模式：light（轻量级）、strict（严格）、off（关闭）
    checkpoint_mode: str = "light"

    # 从哪个检查点恢复执行，None 表示从头开始
    resume_from: Path | None = None

    # 链路追踪模式：on（开启）、off（关闭）
    trace_mode: str = "on"

    # 追踪 ID，用于关联同一任务的所有事件
    trace_id: str | None = None

    # 全局消息列表：跨轮对话的完整消息历史（含 HumanMessage, AIMessage, ToolMessage）
    # 每轮新增 messages 会 append 到这里；rewind 时清空重载；resume 时一次性加载
    global_messages: list[BaseMessage] = field(default_factory=list)

    # 当前会话 ID（agent 运行期间不变，/new 时才更换）
    session_id: str = ""

    # 模型名称/提供方/会话标识等状态信息
    model_name: str = ""
    model_provider: str = ""
    account_name: str = ""

    # Hook 执行引擎，工具执行前后调用
    hook_runner: HookRunner = field(default_factory=HookRunner)

    # 工具输出预算，大输出自动落盘
    result_budget: ToolResultBudget = field(default_factory=ToolResultBudget)

    # 本轮思考模式指令（由 think / ultrathink 等关键词解析注入）
    thinking_instruction: str = ""

    # SessionStart hook 注入的上下文（对齐 Claude Code stdout→context）
    session_context_injection: str = ""

    # Autocompact 连续失败计数（对齐 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3）
    autocompact_failures: int = 0

    def __post_init__(self) -> None:
        """初始化后规范化配置值"""
        self.approval_mode = normalize_approval_mode(self.approval_mode)
        self.agent_mode = normalize_agent_mode(self.agent_mode)
        self.checkpoint_mode = normalize_checkpoint_mode(self.checkpoint_mode)
        self.trace_mode = normalize_trace_mode(self.trace_mode)

    def next_message_id(self) -> str:
        with self._message_seq_lock:
            self.message_seq += 1
            return f"msg-{self.message_seq:05d}"

    def record_read(self, path: Path, *, complete: bool, content: bytes | str | None = None) -> None:
        """记录文件读取快照

        在读取文件后调用此方法，记录文件的修改时间、内容哈希和读取状态。
        后续写入或编辑时会检查快照，确保文件未被外部修改。

        Args:
            path: 文件路径（会被 resolve 为绝对路径）
            complete: 是否完整读取了文件内容
            content: 文件原始字节（用于计算 sha256，须与磁盘一致）。
                传入 str 时会再读磁盘取字节，避免解码后 utf-8 重编码与 on-disk
                字节不一致（BOM / GBK / 换行转换）。为 None 时从磁盘读取。
        """
        resolved = path.resolve()
        if isinstance(content, bytes):
            raw = content
        else:
            # str 或 None：始终用磁盘字节做 hash，与 is_file_modified 对齐
            raw = resolved.read_bytes()
        stat = resolved.stat()
        content_hash = hashlib.sha256(raw).hexdigest()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            content_hash=content_hash,
            complete=complete,
        )

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        """获取指定文件的快照

        Args:
            path: 文件路径

        Returns:
            文件快照，如果未读取过该文件则返回 None
        """
        return self.read_files.get(path.resolve())

    def is_file_modified(self, path: Path) -> bool:
        """检查文件是否被外部修改（mtime + content hash 双重校验）

        先比较 mtime（快速），mtime 一致则认为未修改。
        mtime 不一致时进一步比较 content hash，避免误判（如 touch 但未改内容）。

        Args:
            path: 文件路径

        Returns:
            True 表示文件已被修改（或没有快照），需要重新 read
        """
        snap = self.snapshot_for(path)
        if snap is None:
            return True  # 没有快照，视为已变更
        resolved = path.resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return True  # 文件不存在，视为已变更
        if snap.mtime_ns == stat.st_mtime_ns:
            return False  # mtime 一致，快速判定未修改
        # mtime 不一致，用 content hash 精确判断
        try:
            current_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return True
        return snap.content_hash != current_hash

    def assert_workspace_path(self, path: Path, operation: str = "read") -> Path:
        """安全检查：确保路径在工作区内部且符合安全策略

        防止路径遍历攻击（如 ../../etc/passwd），所有文件操作
        都必须通过此检查后才能执行。

        Args:
            path: 待检查的路径
            operation: 操作类型（"read" / "write" / "delete"）

        Returns:
            解析后的绝对路径

        Raises:
            PathTraversalError: 路径遍历攻击
            PathAccessDeniedError: 访问被拒绝（黑名单/写权限）
        """
        return validate_path_access(self, path, operation)
