"""
审批弹窗模块

当 BashTool 检测到危险命令时，弹出模态对话框请求人工审批。

组件：
- ApprovalGate: 线程间同步原语，工作线程等待主线程的审批结果
- ApprovalModal: Textual 模态对话框，显示命令和风险，等待用户按键

工作流程：
1. 工作线程创建 ApprovalGate(request)
2. 工作线程投递 ApprovalRequestedMessage(gate) 到主线程
3. 主线程弹出 ApprovalModal
4. 用户按 y/n → gate.resolve(approved)
5. 工作线程的 gate.wait() 返回 ApprovalDecision
6. 工作线程根据结果继续或拒绝执行

Textual 框架要点：
- ModalScreen: 模态对话框，覆盖在主界面上方，关闭前阻止其他交互
- compose(): 声明对话框的组件树
- dismiss(value): 关闭对话框并返回值给调用方
- BINDINGS: 键盘快捷键绑定列表
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest


@dataclass
class ApprovalGate:
    """线程间审批同步原语

    工作线程调用 wait() 阻塞等待，主线程调用 resolve() 唤醒。
    内部用 threading.Event 实现阻塞/唤醒。
    """
    request: ApprovalRequest
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        self._ready = Event()

    def resolve(self, approved: bool) -> None:
        reason = "" if approved else "Rejected by human operator."
        self.decision = ApprovalDecision(approved=approved, reason=reason)
        self._ready.set()

    def wait(self) -> ApprovalDecision:
        self._ready.wait()
        return self.decision or ApprovalDecision(approved=False, reason="Approval dialog closed.")


class ApprovalModal(ModalScreen[bool]):
    """审批模态对话框

    显示：工具名、风险原因、工作区路径、待审批命令
    用户操作：y/Enter = 批准，n/Esc = 拒绝

    ModalScreen[bool] 表示 dismiss() 返回 bool 值。
    """
    BINDINGS = [
        ("y", "approve", "Approve"),
        ("enter", "approve", "Approve"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
    }

    ApprovalModal #approval-dialog {
        width: 74;
        max-width: 90%;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }

    ApprovalModal #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    ApprovalModal #approval-command {
        border: tall $panel;
        padding: 1;
        margin: 1 0;
        max-height: 10;
    }

    ApprovalModal #approval-buttons {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, request: ApprovalRequest, workspace: str) -> None:
        super().__init__()
        self.request = request
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        with Container(id="approval-dialog"):
            yield Static(f"Human Approval · {self.request.tool_name}", id="approval-title")
            yield Static(f"Risk: {self.request.risk_reason}")
            yield Static(f"Workspace: {self.workspace}")
            yield Static(Text(self.request.command, overflow="fold"), id="approval-command")
            yield Static("Approve this command?  y/Enter = approve · n/Esc = deny")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Deny", variant="error", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def on_key(self, event: events.Key) -> None:
        if event.key in {"y", "enter"}:
            self.dismiss(True)
        elif event.key in {"n", "escape"}:
            self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
