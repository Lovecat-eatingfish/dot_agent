from datetime import datetime
from pathlib import Path
from uuid import uuid4

# 找到项目的根目录
def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest project root marker from ``start`` upward."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return current


# 创建一个项目的默认工作空间
def default_workspace(root: Path | None = None) -> Path:
    return new_task_workspace(root)


def default_workspace_root(root: Path | None = None) -> Path:
    return (root or find_project_root()) / ".mokioclaw" / "workspaces"


# 创建一个沙箱工作空间
def new_task_workspace(root: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid4().hex[:6]
    return default_workspace_root(root) / f"workspace-{stamp}-{suffix}"
