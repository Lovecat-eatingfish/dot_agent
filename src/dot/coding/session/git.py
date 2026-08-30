"""
dot.coding.session.git — SessionGit 会话级 git 快照

每个 session 一个独立 git 仓库（GIT_DIR 放在 session 目录下），
work-tree 指向 workspace，用于按 turn 快照/回滚 workspace 文件。

生命周期与 session 一致：create 时 init + 基线 commit，
每轮对话末尾 commit_turn，/rewind 时 restore。

提交范围排除：.dot/（session/trace 自身）、.env（含密钥）、.git/（workspace 自身仓库）。
workspace 里已有的 .gitignore 规则天然生效。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_EXCLUDES = (".dot/", ".env", ".git/")


class SessionGit:
    """会话级 git 快照管理（外部 git-dir + work-tree 指向 workspace）"""

    def __init__(self, session_dir: Path, workspace: Path) -> None:
        self._git_dir = session_dir / "git"
        self._work_tree = workspace.resolve()
        self._available = True

    def _git(self, *args: str, check: bool = True) -> str:
        """执行 git 命令（GIT_DIR/GIT_WORK_TREE 指向本会话仓库与 workspace）"""
        import os

        env = os.environ.copy()
        env["GIT_DIR"] = str(self._git_dir)
        env["GIT_WORK_TREE"] = str(self._work_tree)
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True, text=True, env=env,
                cwd=str(self._work_tree), timeout=60,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[session-git] git %s failed: %s", args[0], exc)
            self._available = False
            if check:
                raise RuntimeError(f"git {args[0]} failed: {exc}") from exc
            return ""
        if check and result.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    @property
    def available(self) -> bool:
        """git 是否可用（初始化失败后降级为无快照模式）"""
        return self._available

    def init(self) -> str:
        """初始化仓库并提交 workspace 基线，返回基线 commit hash"""
        if not self._git_dir.is_dir():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "--quiet", str(self._git_dir))
        self._write_excludes()
        return self._commit("baseline")

    def _write_excludes(self) -> None:
        """写入仓库级排除（info/exclude），不污染 workspace"""
        exclude = self._git_dir / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join(_EXCLUDES) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("[session-git] Failed to write excludes: %s", exc)

    def commit_turn(self, turn_id: int) -> str:
        """提交一个 turn 的 workspace 变更，返回 commit hash（无变更则返回当前 HEAD）"""
        return self._commit(f"turn {turn_id}")

    def _commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git(
            "-c", "user.name=dot-agent", "-c", "user.email=dot-agent@local",
            "commit", "--quiet", "--allow-empty", "-m", message,
        )
        return self._git("rev-parse", "HEAD")

    def restore(self, commit: str) -> None:
        """把 workspace 恢复到指定 commit（未跟踪的新文件保留，不执行 clean）"""
        self._git("reset", "--hard", commit)

    def head(self) -> str:
        """当前 HEAD hash（不可用时返回空）"""
        try:
            return self._git("rev-parse", "HEAD", check=False)
        except Exception:
            return ""
