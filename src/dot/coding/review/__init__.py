"""
dot.coding.review — 文件变更评审（事前 diff 审批 + 事后评审回滚）

设计文档：doc/文件变更评审机制设计.md

- previewer : 写操作执行前的 diff 预览（权限 ASK 审批时展示）
- ledger    : 会话级变更台账（登记/接受/回滚，undo 栈 + git 兜底）
- service   : 面向 host/UI/命令的门面
"""
from __future__ import annotations

from .ledger import ChangeLedger, FileChangeRecord
from .previewer import ChangePreview, build_preview
from .service import ReviewService

__all__ = [
    "ChangeLedger",
    "ChangePreview",
    "FileChangeRecord",
    "ReviewService",
    "build_preview",
]
