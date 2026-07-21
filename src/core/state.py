from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.approval import ApprovalRequest, ApprovalDecision


@dataclass
class RuntimeState:
    workspace: Path  # 绝对路径。

    # 审批的操作
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None

    # bash操作的配置项目
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None

    # 作用是安全验证一个路径是否在工作区（workspace）目录内，防止路径遍历攻击或意外访问工作区外的文件。
    def assert_workspace_path(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError(f"path must stay inside workspace: {workspace}")
        return resolved
