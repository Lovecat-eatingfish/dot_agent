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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest, normalize_approval_mode
from mokioclaw.core.checkpoint import normalize_checkpoint_mode
from mokioclaw.core.path_security import PathAccessDeniedError, PathTraversalError, validate_path_access
from mokioclaw.core.trace import normalize_trace_mode


@dataclass(frozen=True)
class FileSnapshot:
    """文件快照，记录文件的修改时间和读取状态

    用途：
    - 在写入或编辑文件前，检查文件是否已被修改（防止覆盖他人的修改）
    - 追踪文件是否被完整读取（用于决定是否允许写入）

    属性：
        path: 文件的绝对路径
        mtime_ns: 文件最后修改时间（纳秒级时间戳），用于检测文件变更
        complete: 文件是否被完整读取（从头到尾），部分读取时不允许写入
    """
    path: Path
    mtime_ns: int
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

    # 审批处理函数，当 approval_mode=inline 时，由 CLI 或 TUI 提供
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None

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

    def __post_init__(self) -> None:
        """初始化后规范化配置值"""
        self.approval_mode = normalize_approval_mode(self.approval_mode)
        self.checkpoint_mode = normalize_checkpoint_mode(self.checkpoint_mode)
        self.trace_mode = normalize_trace_mode(self.trace_mode)

    def record_read(self, path: Path, *, complete: bool) -> None:
        """记录文件读取快照

        在读取文件后调用此方法，记录文件的修改时间和读取状态。
        后续写入或编辑时会检查快照，确保文件未被外部修改。

        Args:
            path: 文件路径（会被 resolve 为绝对路径）
            complete: 是否完整读取了文件内容
        """
        stat = path.stat()
        resolved = path.resolve()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
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
