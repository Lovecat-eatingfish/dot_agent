"""
ToolContext — 工具函数依赖的最小接口（结构化类型）

工具只关心几个核心能力：
  - workspace: 操作的工作目录
  - 文件快照: record_read / snapshot_for / is_file_modified
  - 路径安全: assert_workspace_path
  - 消息序号: next_message_id

Bash 配置（cwd / timeouts / env）和 result_budget 通过 getattr 容错，
不纳入协议强制约束。这样 Session 或任何自定义对象
只要实现核心方法就能当 state 用。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FileSnapshot:
    """文件快照，记录文件的修改时间和内容哈希"""
    path: Path
    mtime_ns: int
    content_hash: str
    complete: bool


@runtime_checkable
class ToolContext(Protocol):
    """工具函数的最小依赖接口"""

    # 身份
    workspace: Path

    # 文件快照
    def record_read(
        self, path: Path, *, complete: bool, content: bytes | str | None = None
    ) -> None: ...
    def snapshot_for(self, path: Path) -> FileSnapshot | None: ...
    def is_file_modified(self, path: Path) -> bool: ...
    def assert_workspace_path(self, path: Path, operation: str = "read") -> Path: ...

    # 消息序号
    def next_message_id(self) -> str: ...
