"""共享 git 工具函数（dot 独立副本，项目级统一入口）

两类仓库：

1. 普通仓库操作（git_init / git_commit / git_reset_hard）
   在指定目录上直接执行，供会话快照等内部目录使用。

2. Agent 专用代码回滚仓库（agent_git_*）
   为支持「用户代码回滚」设计：git 元数据放在 <workspace>/.dot/git/，
   work-tree 指向用户项目目录。这样：
   - 用户项目目录内容被完整快照，rewind 时可恢复用户代码
   - 不碰用户项目自己的 .git（零侵入）
   - .dot/（agent 全部元数据）通过 info/exclude 排除，不进快照
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ============================================================
# 普通仓库操作
# ============================================================

def git_init(workspace: Path) -> None:
    """在 workspace 中初始化 git 仓库（已存在时 git init 是安全的幂等操作）。"""
    _run(["git", "init"], cwd=workspace)


def git_commit(workspace: Path, message: str) -> str:
    """git add . && git commit -m {message}，返回 commit hash。"""
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", message], cwd=workspace)
    result = _run(["git", "rev-parse", "HEAD"], cwd=workspace)
    return result.stdout.strip()


def git_reset_hard(workspace: Path, commit_hash: str) -> None:
    """git reset --hard {commit_hash}。"""
    _run(["git", "reset", "--hard", commit_hash], cwd=workspace)


# ============================================================
# Agent 专用代码回滚仓库
# ============================================================

def agent_git_dir(workspace: Path) -> Path:
    """agent 专用 repo 的 GIT_DIR 位置：<workspace>/.dot/git"""
    return workspace / ".dot" / "git"


def _agent_args(workspace: Path) -> list[str]:
    return [
        "git",
        f"--git-dir={agent_git_dir(workspace)}",
        f"--work-tree={workspace}",
    ]


def agent_git_init(workspace: Path) -> None:
    """初始化 agent 专用 repo（幂等），并排除 .dot/ 自身。

    - git init 到 .dot/git/（目录不存在则创建）
    - info/exclude 写入 ".dot/"，避免 agent 元数据进代码快照
    """
    git_dir = agent_git_dir(workspace)
    if (git_dir / "HEAD").exists():
        return
    git_dir.mkdir(parents=True, exist_ok=True)
    # --git-dir 形式的 init：仓库直接位于 .dot/git（而不是 .dot/git/.git）
    _run(_agent_args(workspace) + ["init"])
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if exclude.exists():
        existing = exclude.read_text(encoding="utf-8", errors="replace")
    if ".dot/" not in existing:
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n# agent metadata, never snapshot\n.dot/\n")


def agent_git_commit(workspace: Path, message: str) -> str:
    """把用户项目目录当前状态提交到 agent repo，返回 commit hash。

    无变更时 commit 失败（nothing to commit）→ 返回上一次的 HEAD
    （或空串表示仓库尚无提交）。
    """
    agent_git_init(workspace)
    args = _agent_args(workspace)
    _run([*args, "add", "-A"])
    commit = _run([*args, "commit", "-m", message])
    head = _run([*args, "rev-parse", "HEAD"])
    if head.returncode != 0:
        return ""
    return head.stdout.strip()


def agent_git_reset_hard(workspace: Path, commit_hash: str) -> None:
    """把用户项目目录恢复到指定 commit（rewind 用）。

    只恢复 tracked 文件；rewind 之后新产生但未随更早轮次提交的
    untracked 文件不会被删除（保守策略，避免误删用户文件）。
    """
    _run([*_agent_args(workspace), "reset", "--hard", commit_hash])


def agent_git_has_commit(workspace: Path, commit_hash: str) -> bool:
    """校验 agent repo 中存在指定 commit"""
    result = _run(
        [*_agent_args(workspace), "cat-file", "-e", f"{commit_hash}^{{commit}}"],
        check=False,
    )
    return result.returncode == 0
