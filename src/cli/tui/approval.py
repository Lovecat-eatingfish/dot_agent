from dataclasses import dataclass
from threading import Event

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from core.approval import ApprovalRequest, ApprovalDecision


# ┌─────────────────────────────────────────────────────────────────┐
# │                    主流程 (Agent 线程)                         │
# │                                                               │
# │  1. 创建 ApprovalGate(request)                                │
# │  2. decision = gate.wait()  ← 线程阻塞在这里                  │
# │                                                               │
# └─────────────────────────────────────────────────────────────────┘
#                               │
#                               ▼  UI 线程
# ┌─────────────────────────────────────────────────────────────────┐
# │  3. 显示 ApprovalModal 弹窗                                   │
# │  4. 用户操作 → dismiss(True/False)                           │
# │  5. 调用 gate.resolve(approved)  ← 释放主流程线程            │
# │  6. 主流程拿到 decision，继续执行                             │
# └─────────────────────────────────────────────────────────────────┘
@dataclass
class ApprovalGate:
    request: ApprovalRequest
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        # 线程同步的工具
        self._ready = Event()

    # 由外部调用（比如 CLI 交互中用户输入了 y/n）。
    # 创建审批结果对象，记录是否批准及理由（拒绝时有默认理由）。
    # 调用 _ready.set() 释放所有等待中的线程。
    def resolve(self, approved: bool) -> None:
        reason = "" if approved else "Rejected by human operator."
        self.decision = ApprovalDecision(approved=approved, reason=reason)
        self._ready.set()  # 通知所有等待的线程：结果已就绪

    def wait(self) -> ApprovalDecision:
        self._ready.wait()  # 阻塞直到 _ready.set() 被调用
        return self.decision or ApprovalDecision(approved=False, reason="Approval dialog closed.")


# ApprovalModal (前端UI)
# 1. ApprovalModal 类 —— 终端审批弹窗
# 继承自 Textual 的 ModalScreen，是一个模态弹窗（必须关闭才能继续操作）。
# 泛型 [bool] 表示这个屏幕关闭时返回一个布尔值（批准/拒绝）。
class ApprovalModal(ModalScreen[bool]):
    # 用户可以用键盘快速操作，不需要鼠标。
    BINDINGS = [
        ("y", "approve", "Approve"),  # 按 y 批准
        ("enter", "approve", "Approve"),  # 按 Enter 批准
        ("n", "deny", "Deny"),  # 按 n 拒绝
        ("escape", "deny", "Deny"),  # 按 Esc 拒绝
    ]

    # 定义了弹窗的外观：居中显示、橙色主题、命令区域有边框且最多显示10行。
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

    # 5. compose —— 构建 UI 组件
    # ┌─────────────────────────────────────────────────────┐
    # │  ⚠ Human Approval · delete_file                    │
    # │  Risk: 将永久删除生产数据库文件                      │
    # │  Workspace: /home/user/project                     │
    # │  ┌─────────────────────────────────────────────────┐ │
    # │  │ rm -rf /data/production.db                     │ │
    # │  └─────────────────────────────────────────────────┘ │
    # │  Approve this command?  y/Enter = approve · n/Esc = deny │
    # │  [Approve]    [Deny]                                │
    # └─────────────────────────────────────────────────────┘
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

    # 鼠标点击按钮
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    # 键盘按键
    def on_key(self, event: events.Key) -> None:
        if event.key in {"y", "enter"}:
            self.dismiss(True)
        elif event.key in {"n", "escape"}:
            self.dismiss(False)

    # Action 命令（由 BINDINGS 触发）
    # 按 y 或 Enter 触发 action_approve
    # 按 n 或 Esc 触发 action_deny
    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
