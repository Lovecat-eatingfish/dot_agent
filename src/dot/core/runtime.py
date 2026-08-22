"""
Agent 运行时状态（dot 裁剪版）

RuntimeState 是工具执行的核心状态容器：
- 文件快照：追踪已读取文件的状态，防止覆盖并发修改（先读后写）
- 运行时配置：工作区路径、审批模式、bash 超时等
- MCP 渐进披露：已按需加载的工具表

相比旧版裁剪掉：checkpoint / trace / global_messages / model 信息 /
thinking 指令 / autocompact 计数（这些职责已归 Session 或后续模块）。
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .hooks import HookRunner
from .path_security import validate_path_access
from .tool_result_budget import ToolResultBudget

VALID_AGENT_MODES = {"auto", "plan", "approve", "edit"}


def normalize_agent_mode(mode: str | None) -> str:
    normalized = (mode or "auto").strip().lower()
    return normalized if normalized in VALID_AGENT_MODES else "auto"


@dataclass(frozen=True)
class FileSnapshot:
    """文件快照，记录文件的修改时间和内容哈希

    - 写入/编辑前检查文件是否被外部修改
    - 追踪文件是否被完整读取（部分读取不允许写入）
    """
    path: Path
    mtime_ns: int
    content_hash: str
    complete: bool


@dataclass
class RuntimeState:
    """Agent 运行时状态，贯穿整个任务执行生命周期"""

    # 工作区根目录，所有文件操作都必须在此目录内
    workspace: Path

    # 已读取文件的快照表，key 是文件绝对路径
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)

    # （已废弃）审批统一由 core.permission.PermissionManager 管理；
    # 字段保留仅为兼容旧引用，不再参与权限决策
    approval_mode: str = "inline"

    # Agent 运行模式：auto / plan / approve / edit
    agent_mode: str = "auto"

    # 工具权限规则：支持精确名称与通配符（如 mcp__*）
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    # （已废弃）审批统一由 PermissionManager 管理，字段仅为兼容保留
    approval_handler: Callable[[Any], Any] | None = None

    # 递增消息序号（并行工具调用时加锁）
    message_seq: int = 0
    _message_seq_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # MCP 渐进披露：已按需加载的工具名 → StructuredTool
    loaded_mcp_tools: dict[str, Any] = field(default_factory=dict)

    # ToolSearch 延迟加载：已加载的延迟工具
    loaded_tools: dict[str, Any] = field(default_factory=dict)

    # BashTool 可变更的工作目录（相对/绝对，须在 workspace 内）
    cwd: Path | None = None

    # BashTool 默认超时（秒）
    bash_default_timeout_seconds: int = 120

    # BashTool 最大允许超时（秒）
    bash_max_timeout_seconds: int = 600

    # BashTool 输出截断阈值（字符数）
    bash_max_output_chars: int = 6000

    # 额外的环境变量文件路径，执行 Bash 命令时加载
    bash_env_file: Path | None = None

    # 额外注入的环境变量
    extra_env: dict[str, str] = field(default_factory=dict)

    # 当前会话 ID
    session_id: str = ""

    # Hook 执行引擎，工具执行前后调用
    hook_runner: HookRunner | None = None

    # 工具输出预算，大输出自动落盘
    result_budget: ToolResultBudget = field(default_factory=ToolResultBudget)

    def __post_init__(self) -> None:
        self.agent_mode = normalize_agent_mode(self.agent_mode)

    def next_message_id(self) -> str:
        with self._message_seq_lock:
            self.message_seq += 1
            return f"msg-{self.message_seq:05d}"

    def record_read(self, path: Path, *, complete: bool, content: bytes | str | None = None) -> None:
        """记录文件读取快照

        Args:
            path: 文件路径（resolve 为绝对路径）
            complete: 是否完整读取
            content: 文件原始字节（用于 sha256）。str/None 时从磁盘读字节，
                避免解码重编码与磁盘不一致（BOM / GBK / 换行转换）。
        """
        resolved = path.resolve()
        if isinstance(content, bytes):
            raw = content
        else:
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
        """获取指定文件的快照（未读取过返回 None）"""
        return self.read_files.get(path.resolve())

    def is_file_modified(self, path: Path) -> bool:
        """检查文件是否被外部修改（mtime 快检 + content hash 精确判定）"""
        snap = self.snapshot_for(path)
        if snap is None:
            return True  # 没有快照，视为已变更
        resolved = path.resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return True
        if snap.mtime_ns == stat.st_mtime_ns:
            return False
        try:
            current_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return True
        return snap.content_hash != current_hash

    def assert_workspace_path(self, path: Path, operation: str = "read") -> Path:
        """安全检查：确保路径在工作区内部且符合安全策略

        Raises:
            PathTraversalError: 路径遍历攻击
            PathAccessDeniedError: 访问被拒绝（黑名单/写权限）
        """
        return validate_path_access(self, path, operation)
