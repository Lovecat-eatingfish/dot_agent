"""
智能工作区检测模块

实现类似 Claude Code 的工作区自动检测：
1. 如果用户指定了文件路径，以文件所在目录为工作区
2. 如果用户在某个项目目录下，自动检测项目根目录
3. 如果没有打开文件，使用当前工作目录

优先级：
- 用户显式指定的 --workspace 参数
- 打开的文件路径（通过 TUI 传入）
- 项目根目录（pyproject.toml / .git）
- 当前工作目录
- 默认 .mokioclaw/workspaces/
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def detect_workspace_from_file(file_path: Path | None = None) -> Path | None:
    """从打开的文件路径检测工作区

    Args:
        file_path: 用户打开的文件路径

    Returns:
        检测到的工作区，如果无法检测则返回 None
    """
    if file_path is None:
        return None

    file_path = Path(file_path).resolve()

    # 如果是文件，取其父目录
    # 使用 exists() 而不是 is_file() 避免符号链接等问题
    if file_path.exists() and file_path.is_file():
        return file_path.parent

    # 如果是目录，直接返回
    if file_path.exists() and file_path.is_dir():
        return file_path

    # 路径不存在但看起来像文件路径，取其父目录
    # 这处理了"用户指定了一个可能不存在的文件"的情况
    if not file_path.exists():
        # 检查是否有文件扩展名（简单判断是否是文件）
        if file_path.suffix:
            return file_path.parent
        # 否则可能是目录
        return file_path

    return None


def detect_workspace_from_project(start: Path | None = None) -> Path:
    """从项目根目录检测工作区

    查找包含以下标记的目录：
    - pyproject.toml
    - .git
    - package.json
    - Cargo.toml
    - go.mod

    Args:
        start: 起始路径，默认为当前工作目录

    Returns:
        项目根目录（如果找到），否则返回起始路径
    """
    current = (start or Path.cwd()).resolve()

    # 如果当前路径本身就有项目标记，直接返回
    if _has_project_marker(current):
        return current

    # 向上查找
    for candidate in current.parents:
        # 排除一些不应该作为项目根的目录
        if candidate.name in {"node_modules", ".venv", "venv", "env", ".git"}:
            continue

        if _has_project_marker(candidate):
            return candidate

    # 没找到项目根，返回起始路径
    return current


def _has_project_marker(path: Path) -> bool:
    """检查路径是否有项目标记文件"""
    markers = {
        "pyproject.toml",
        ".git",
        "package.json",
        "Cargo.toml",
        "go.mod",
    }
    return any((path / marker).exists() for marker in markers)


def resolve_workspace(
    user_specified: Path | None = None,
    opened_file: Path | None = None,
    fallback: Path | None = None,
) -> Path:
    """智能解析工作区

    优先级（从高到低）：
    1. 用户通过 --workspace 指定的目录
    2. 打开的文件所在目录
    3. 项目根目录（自动检测）
    4. 当前工作目录
    5. fallback 或默认 .mokioclaw/workspaces/

    Args:
        user_specified: 用户显式指定的工作区
        opened_file: 用户打开的文件路径（来自 TUI/IDE）
        fallback: 备用工作区

    Returns:
        解析后的工作区路径
    """
    # 优先级 1：用户显式指定
    if user_specified is not None:
        workspace = user_specified.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    # 优先级 2：打开的文件
    if opened_file is not None:
        file_workspace = detect_workspace_from_file(opened_file)
        if file_workspace is not None:
            file_workspace.mkdir(parents=True, exist_ok=True)
            return file_workspace

    # 优先级 3：项目根目录
    project_root = detect_workspace_from_project()
    # 如果找到的项目根不同于当前工作目录，使用它
    if project_root != Path.cwd():
        project_root.mkdir(parents=True, exist_ok=True)
        return project_root

    # 优先级 4：当前工作目录
    cwd = Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def is_path_under_workspace(path: Path, workspace: Path) -> bool:
    """检查路径是否在工作区下

    Args:
        path: 要检查的路径
        workspace: 工作区路径

    Returns:
        是否在工作区内
    """
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def get_relative_path(path: Path, workspace: Path) -> Path:
    """获取路径相对于工作区的相对路径

    Args:
        path: 绝对路径
        workspace: 工作区路径

    Returns:
        相对路径
    """
    try:
        return path.resolve().relative_to(workspace.resolve())
    except ValueError:
        # 不在工作区内，返回原路径
        return path
