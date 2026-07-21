from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.approval import ApprovalRequest, ApprovalDecision


# 文件的快照对象
@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    mtime_ns: int
    complete: bool


@dataclass
class RuntimeState:
    workspace: Path  # 绝对路径。

    # 审批的模式
    approval_mode: str = "inline"

    # 审批的操作
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None

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
