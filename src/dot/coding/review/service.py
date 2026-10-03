"""
dot.coding.review.service — ReviewService 事后评审门面

对 UI/命令层暴露的三件事：
1. hook 入口：before_tool_call / after_tool_call / discard（host 组合进工具链）
2. turn 收尾：checkpoint_turn —— 无头环境（auto_resolve=True）自动接受 pending，
   交互环境保持 pending 等用户裁决
3. /review 命令语义：list / show / accept / revert / auto

对 host 暴露 pending 计数（状态栏）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from dot.agent.tools import AgentToolResult
from dot.ai.types import ToolCall

from .fsutil import truncate_diff
from .ledger import ChangeLedger, FileChangeRecord


class ReviewService:
    """事后评审门面（host 持有一个实例）"""

    def __init__(
        self,
        workspace: Path,
        git_provider: Callable[[], object | None] | None = None,
        *,
        auto_resolve: bool = True,
    ) -> None:
        self.ledger = ChangeLedger(workspace, git_provider)
        self.auto_resolve = auto_resolve  # 无头环境 True；TUI/Console 启动时置 False

    # ============================================================
    # hook 入口（host 组合）
    # ============================================================

    def before_tool_call(self, call: ToolCall) -> None:
        self.ledger.before(call)

    def discard_tool_state(self, call_id: str) -> None:
        self.ledger.discard(call_id)

    def after_tool_call(self, call: ToolCall, result: AgentToolResult, is_error: bool) -> None:
        self.ledger.after(call, result, is_error)

    # ============================================================
    # turn 收尾 / 会话生命周期
    # ============================================================

    def checkpoint_turn(self, turn_id: int) -> list[FileChangeRecord]:
        """turn 收尾：回填 turn_id；无头环境自动接受 pending。返回本轮新增记录"""
        fresh = self.ledger.checkpoint_turn(turn_id)
        if self.auto_resolve and fresh:
            self.ledger.auto_resolve_pending()
        return fresh

    def reset_session(self) -> None:
        self.ledger.reset_session()

    def on_rewind(self, turn_id: int) -> int:
        return self.ledger.on_rewind(turn_id)

    @property
    def pending_count(self) -> int:
        return len(self.ledger.pending_records())

    # ============================================================
    # /review 命令语义
    # ============================================================

    def list_text(self) -> str:
        pending = self.ledger.pending_records()
        if not pending:
            return "no pending file changes"
        lines = []
        for r in pending:
            turn = f"turn {r.turn_id}" if r.turn_id else "current"
            fallback = " [git-only]" if r.fallback_git else ""
            lines.append(
                f"  #{r.id:<3} {turn:<9} {r.path}  (+{r.lines_added}/-{r.lines_removed})"
                f"  {r.tool_name}{fallback}"
            )
        return f"pending file changes ({len(pending)}):\n" + "\n".join(lines)

    def show_text(self, record_id: int | None) -> str:
        """单条（或唯一一条 pending）完整 diff"""
        if record_id is None:
            pending = self.ledger.pending_records()
            if len(pending) != 1:
                return "usage: /review show <id>"
            record = pending[0]
        else:
            record = next((r for r in self.ledger.records if r.id == record_id), None)
            if record is None:
                return f"no such change: #{record_id}"
        diff, truncated = truncate_diff(record.diff, max_lines=400, max_chars=16000)
        header = (
            f"#{record.id} {record.kind} {record.path} "
            f"[{record.status}, turn {record.turn_id or 'current'}] "
            f"(+{record.lines_added}/-{record.lines_removed})"
        )
        body = diff if diff else "(no text diff — binary or no change)"
        suffix = "\n… (diff truncated)" if truncated else ""
        return header + "\n" + body + suffix

    def accept(self, target: str) -> str:
        if target == "all":
            count = self.ledger.accept_all()
            return f"accepted {count} change(s)" if count else "no pending changes"
        record_id = _parse_id(target)
        if record_id is None:
            return "usage: /review accept <id|all>"
        return self.ledger.accept(record_id)

    def revert(self, target: str) -> str:
        if target == "all":
            return self.ledger.revert_all()
        record_id = _parse_id(target)
        if record_id is None:
            return "usage: /review revert <id|all>"
        return self.ledger.revert(record_id)

    # ============================================================
    # 汇总展示（turn 结束 / 状态栏）
    # ============================================================

    def pending_summary_text(self) -> str:
        """turn 结束的一行式汇总；无 pending 返回空串"""
        pending = self.ledger.pending_records()
        if not pending:
            return ""
        files = ", ".join(f"{r.path} (+{r.lines_added}/-{r.lines_removed})" for r in pending[:4])
        more = f" …(+{len(pending) - 4} more)" if len(pending) > 4 else ""
        return f"· {len(pending)} file change(s) pending review: {files}{more}  (/review to inspect)"


def _parse_id(text: str | int) -> int | None:
    """id 解析：容忍 int 直传与 '#N' / 'N' 字符串形态"""
    if isinstance(text, int):
        return text
    stripped = text.strip().lstrip("#")
    try:
        return int(stripped)
    except ValueError:
        return None
