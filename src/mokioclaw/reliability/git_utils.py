"""共享 git 工具函数（项目级，统一入口）

提供三个核心操作，所有模块通过此处间接调用 git，不直接 subprocess：
  - git_init      初始化仓库
  - git_commit    暂存 + 提交，返回 commit hash
  - git_reset_hard 硬重置到指定 commit
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git_init(workspace: Path) -> None:
    """在 workspace 中初始化 git 仓库。"""
    subprocess.run(
        ["git", "init"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def git_commit(workspace: Path, message: str) -> str:
    """git add . && git commit -m {message}，返回 commit hash。

    Args:
        workspace: 仓库根目录
        message: commit message
    """
    subprocess.run(
        ["git", "add", "."],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_reset_hard(workspace: Path, commit_hash: str) -> None:
    """git reset --hard {commit_hash}。"""
    subprocess.run(
        ["git", "reset", "--hard", commit_hash],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
