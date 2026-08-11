"""
Textual TUI 主应用

基于 Textual 框架的终端 UI，支持多轮会话交互。

Textual 框架要点：
- App: 应用基类，类似 Web 框架的 Application
- compose(): 声明组件树（类似 React 的 render）
- CSS: 类似网页 CSS 的样式系统，用 Textual 选择器
- Message: 组件间通信的消息类
- post_message(): 从任意线程投递消息到主事件循环
- handle_<MessageName>(): 消息处理函数（命名约定）

线程模型：
- 主线程：Textual 事件循环，处理 UI 更新和用户输入
- 工作线程：执行 stream_session_events()，产生 agent 事件
- 通过 Message 在两个线程间通信

Textual主线程（UI线程）：渲染界面、接收键盘输入、弹窗，不能跑耗时逻辑
后台工作线程：执行Agent `stream_session_events()`，跑大模型、调用工具、循环思考

⚠️ 严禁子线程直接操作 UI 组件，Textual 会直接崩溃。
解决方案：自定义Message消息类，子线程调用post_message()投递消息，主线程回调on_xxx_message()更新界面



消息类型： 3 种自定义消息，作为线程之间的信使：
AgentEventMessage：后台产出一条 agent 事件，发给 UI 去渲染
RunFinishedMessage：agent 任务跑完 / 中断，通知 UI 恢复输入框
ApprovalRequestedMessage：遇到高危 bash 命令，通知 UI 弹出确认弹窗

Message是 Textual 内置跨线程消息基类；
子线程：self.call_from_thread(self.post_message, AgentEventMessage(event))
主线程接收：def on_agent_event_message(self, message: AgentEventMessage):
call_from_thread：专门给工作线程用，把消息安全投递到 UI 事件循环，千万不要直接子线程操作 DOM 组件。



"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Literal

from rich.pretty import Pretty
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Footer, Header, Input, Static

from mokioclaw.cli.event_summary import EventSummary, shorten, summarize_event
from mokioclaw.cli.tui.approval import ApprovalGate, ApprovalModal
from mokioclaw.cli.tui.logo import render_logo
from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.agent import stream_session_events
from mokioclaw.core.paths import default_workspace

# 流工厂类型：创建 agent 事件流的可调用对象
StreamFactory = Callable[..., Iterable[dict[str, Any]]]


class AgentEventMessage(Message):
    """工作线程 → 主线程：agent 事件消息

    工作线程每产生一个事件，通过 post_message(AgentEventMessage(event))
    投递到主线程，主线程的 handle_agent_event_message() 处理 UI 更新。
    """

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self.event = event


class RunFinishedMessage(Message):
    """工作线程 → 主线程：agent 执行完成消息

    工作线程完成或异常退出时投递，主线程更新状态栏。
    """

    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status


class ApprovalRequestedMessage(Message):
    """工作线程 → 主线程：审批请求消息

    当 BashTool 检测到危险命令时，工作线程创建 ApprovalGate
    并投递此消息，主线程弹出 ApprovalModal 模态对话框。
    """

    def __init__(self, gate: ApprovalGate) -> None:
        super().__init__()
        self.gate = gate


class MokioClawTuiApp(App[None]):
    """Textual TUI 主应用

    事件流
用户输入任务，按下回车
    ↓
on_input_submitted → start_task()
    ↓
run_worker 新开后台线程 → _run_stream()
    ↓
stream_session_events() 执行Agent逻辑，循环yield事件
    ↓
子线程 call_from_thread post_message(AgentEventMessage(event))
    ↓
主线程 on_agent_event_message()
    ↓
├─ _update_state_from_event 更新内存状态(todos、计数器、路径)
└─ _write_summary → _mount_event_card() 渲染UI卡片

遇到高危bash命令：
    后台线程调用 _approval_handler
    创建 ApprovalGate，投递 ApprovalRequestedMessage
    主线程弹出 ApprovalModal弹窗
    用户点击 Yes / No → gate.resolve(bool)
    后台线程gate.wait()解除阻塞，继续执行agent

Agent全部执行完毕
    ↓
子线程发送 RunFinishedMessage
    ↓
主线程 on_run_finished_message
    running=False，输入框解禁，刷新侧边栏



    布局：
    ┌─────────────────────────────────────┐
    │ Header (时钟)                        │
    ├─────────────────────────────────────┤
    │ Logo │ MokioClaw (标题)              │
    │      │ status: running              │
    ├──────┴──────────────────────────────┤
    │ 事件列表 (可滚动)  │ 侧边栏 (会话)   │
    │ > Planner ...      │ Session         │
    │ > Tool Call ...    │ todos: 3/5      │
    │ > Verifier ...     │ turn: 2         │
    ├─────────────────────────────────────┤
    │ ❯ 输入框                            │
    ├─────────────────────────────────────┤
    │ Footer (快捷键提示)                  │
    └─────────────────────────────────────┘

    线程安全状态（通过 _state_lock 保护）：
    - running: 是否有 agent 正在执行
    - todos: 当前待办事项列表
    - 最新 workspace/checkpoint/trace 路径
    """
    CSS = """
    Screen {
        background: #101113;
        color: #d7d1c9;
    }

    #root {
        height: 1fr;
    }

    #top {
        height: 8;
        border-bottom: solid #2f3437;
        padding: 0 2;
        background: #151719;
    }

    #logo {
        width: 32;
        height: 7;
        content-align: center middle;
    }

    #title-block {
        width: 1fr;
        height: 7;
        padding-left: 2;
        content-align: left middle;
    }

    #title {
        text-style: bold;
        color: #f3ede3;
    }

    #status {
        color: #9aa4a6;
    }

    #subtitle {
        color: #7fd6c2;
    }

    #body {
        height: 1fr;
    }

    #events {
        width: 1fr;
        height: 100%;
        border-right: solid #2f3437;
        padding: 1 1;
        background: #101113;
    }

    #sidebar {
        width: 36;
        min-width: 30;
        height: 100%;
        padding: 1 1;
        background: #151719;
    }

    #side-title {
        text-style: bold;
        color: #f4bf75;
        margin-bottom: 1;
    }

    #side-state {
        color: #d7d1c9;
    }

    #input-row {
        height: 3;
        border: round #4a8f86;
        padding: 0 1;
        background: #151719;
    }

    #prompt {
        width: 3;
        height: 1;
        content-align: center middle;
        color: #7fd6c2;
        text-style: bold;
    }

    #task-input {
        width: 1fr;
        height: 1;
        border: none;
        background: #151719;
        color: #f3ede3;
    }

    #hint {
        color: #8a9294;
        width: 32;
        height: 1;
        padding-left: 1;
        content-align: right middle;
    }

    .event-card {
        height: auto;
        min-height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
        border-left: solid #3f474b;
    }

    .event-summary {
        height: auto;
        min-height: 1;
    }

    .event-running {
        border-left: solid #f4bf75;
    }

    .event-success {
        border-left: solid #7fd68a;
    }

    .event-error {
        border-left: solid #ef6f6c;
    }

    .event-info {
        border-left: solid #7fd6c2;
    }

    .event-user {
        border-left: solid #f4bf75;
        background: #222426;
    }

    .detail {
        height: auto;
        max-height: 12;
        color: #b7b0a8;
        padding: 0 1 1 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "cancel_or_quit", "Cancel/Quit"),
        ("ctrl+l", "clear_events", "Clear"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
            self,
            *,
            # ========== 第一类：核心任务参数 ==========
            initial_task: str | None = None,
            # TUI 启动后自动执行的初始任务
            # 来自 CLI 层 tui 命令的位置参数：mokioclaw tui "帮我写个计算器"
            # 在 on_mount() 里如果不为 None，会自动调用 start_task() 执行，用户不用手动输入
            # 默认 None：只打开 TUI 不立即执行任务，等待用户手动输入

            resume: Path | None = None,
            # 从已有的 checkpoint 恢复之前的会话
            # 来自 CLI 层的 --resume 选项：mokioclaw tui --resume ./workspaces/xxx
            # 传递给 resolve_workspace() 作为 fallback，同时传给 stream_session_events() 作为 resume_workspace
            # 恢复时会读取之前的 checkpoint 快照、上下文、会话历史，断点续跑
            # 默认 None：创建全新会话

            # ========== 第二类：工作区控制参数 ==========
            workspace: Path | None = None,
            # 用户显式指定的工作区根目录
            # 来自 CLI 层的 --workspace / -w 选项
            # 在 resolve_workspace() 里优先级最高：只要用户传了这个，直接用它，不自动检测
            # 所有工具（BashTool、ReadFileTool 等）的文件操作都会限制在这个目录下
            # 默认 None：进入自动检测逻辑

            opened_file: Path | None = None,
            # 用户当前打开的文件路径，用来模拟 Claude Code 的行为
            # 对应 workspace_detection.py 的 detect_workspace_from_file()
            # 优先级仅次于 user_specified：如果传了这个，自动取它的父目录作为工作区
            # 典型场景：用户在 VS Code 里打开了 ./src/main.py，IDE 把这个路径传给 TUI
            # TUI 自动把 ./src 设为工作区，而不是项目根目录，对齐 Claude Code 的交互逻辑
            # 默认 None：不基于文件路径检测工作区

            # ========== 第三类：执行控制参数 ==========
            max_attempts: int = 3,
            # Planner→Actor→Verifier 循环的最大重试次数
            # 来自 CLI 层的 --max-attempts 选项
            # 传给 build_complex_workflow() 的配置，决定循环多少次后强制进入 Final 节点
            # 在 _stream_complex_workflow() 的 inputs 里作为 max_attempts 传入 LangGraph 状态
            # 默认 3：平衡尝试次数和成本，避免无限循环

            approval_mode: Literal["inline", "auto", "deny"] = "inline",
            # 高危 Bash 命令的审批模式
            # 来自 CLI 层的 --approval-mode 选项
            # inline（默认）：遇到高危命令弹窗让用户确认，安全优先
            # auto：自动批准所有高危命令，适合完全信任的场景
            # deny：直接拒绝所有高危命令，适合严格限制的场景
            # 在 _run_stream() 里，如果模式是 inline，会把 self._approval_handler 传给 agent
            # 这个 handler 会弹 ApprovalModal 对话框等待用户选择
            # 为什么需要：防止 Agent 误执行删除文件、安装依赖等危险命令

            checkpoint_mode: Literal["light", "strict", "off"] = "light",
            # 检查点保存策略
            # 来自 CLI 层的 --checkpoint-mode 选项
            # 传给 CheckpointManager 和 normalize_checkpoint_mode()
            # light（默认）：只保存状态摘要、事件列表，体积小
            # strict：保存完整状态快照 + git diff，体积大但恢复更准确
            # off：不保存检查点，不能恢复
            # 默认 light：平衡恢复能力和存储开销

            trace_mode: Literal["on", "off"] = "on",
            # 链路追踪开关
            # 来自 CLI 层的 --trace-mode 选项
            # 传给 TraceRecorder 和 normalize_trace_mode()
            # 开启时生成 trace/ 目录，记录每个节点的输入输出、耗时、事件时间线，用于调试和审计
            # 关闭时不生成追踪文件，减少磁盘开销
            # 默认 on：方便调试，生产环境可以关

            # ========== 第四类：内部扩展参数 ==========
            stream_factory: StreamFactory = stream_session_events,
            # Agent 事件流的工厂函数
            # StreamFactory 类型：Callable[..., Iterable[dict[str, Any]]]
            # 默认值是 stream_session_events，也就是生产环境用的真实 Agent 流
            # 在 _run_stream() 里调用：for event in self.stream_factory(...)
            # 为什么设计成可注入：依赖注入原则，方便测试（传假 factory 返回预设事件流）和扩展（云端流等）
    ) -> None:
        """
        TUI 会话的全局配置入口，所有影响 TUI 运行行为的参数都在这里注入。

        参数分四类：
        1. 核心任务参数：控制做什么、是否恢复
        2. 工作区控制参数：控制在哪里做、基于哪个文件
        3. 执行控制参数：控制重试几次、要不要审批、保存多少日志
        4. 内部扩展参数：控制流来源（真实 Agent 还是测试用的假流）

        每个参数都对应到下层的一个具体模块，最终影响 Agent 的执行行为或者 TUI 的交互逻辑。
        """
        super().__init__()
        self.initial_task = initial_task
        # 智能工作区解析：支持打开的文件作为工作区
        from mokioclaw.core.workspace_detection import resolve_workspace

        # 工作目录
        self.workspace = resolve_workspace(
            user_specified=workspace,
            opened_file=opened_file,
            fallback=resume,
        )
        self.session_workspace = self.workspace
        self.max_attempts = max_attempts
        self.approval_mode = approval_mode
        self.checkpoint_mode = checkpoint_mode
        self.trace_mode = trace_mode
        self.resume = resume
        self.stream_factory = stream_factory
        self.running = False
        self.run_count = 0
        self.approval_count = 0
        self.failed_tool_count = 0
        self.tool_count = 0
        self.latest_workspace = str(self.session_workspace)
        self.latest_checkpoint = ""
        self.latest_trace = ""
        self.session_id = ""
        self.session_turn = 0
        self.last_route = ""
        self.sidebar_text = ""
        self.todos: list[dict[str, Any]] = []
        # self._state_lock = Lock()：线程锁！后台线程和 UI 线程都会读写todos等状态，加锁防止并发读写错乱。
        self._state_lock = Lock()

    # 2. compose() → 构建 UI 组件树
    def compose(self) -> ComposeResult:
        """
        Textual 的compose()等价于前端render()，用yield产出组件，布局结构看代码注释里的 ASCII 图。
        组件划分：
            Header：顶部标题栏，带时钟
            top 区域：logo + 标题、运行状态文字
            body 横向布局：
                左边：VerticalScroll(id="events") 事件滚动列表，所有 agent 输出卡片放这里
                右边：侧边栏，展示会话统计信息
            input-row：底部输入框，用户在这里输入任务
            Footer：底部快捷键提示
                文件里一大段CSS = ，Textual 内置 CSS，和网页 CSS 语法几乎一样，控制颜色、边框、背景、布局。
        """
        yield Header(show_clock=True)
        with Vertical(id="root"):
            with Horizontal(id="top"):
                yield Static(render_logo(max_width=28, max_rows=7), id="logo")
                with Vertical(id="title-block"):
                    yield Static("MokioClaw", id="title")
                    yield Static("ready", id="status")
                    yield Static("coding session with context + harness", id="subtitle")
            with Horizontal(id="body"):
                yield VerticalScroll(id="events")
                with Vertical(id="sidebar"):
                    yield Static("Session", id="side-title")
                    yield Static("", id="side-state")
            with Horizontal(id="input-row"):
                yield Static("❯", id="prompt")
                yield Input(placeholder="Chat or ask for coding work, then press Enter", id="task-input")
                yield Static("Enter send · /new session · Ctrl+L clear", id="hint")
        yield Footer()

    # 页面挂在调用
    def on_mount(self) -> None:
        """
        组件挂载完成后的生命周期钩子，等价于前端on_mounted。
            输出欢迎信息，刷新右侧侧边栏；
            输入框自动聚焦；
            如果启动 TUI 时传了initial_task初始任务，直接自动跑任务。
        :return:
        """
        self._write_welcome()
        self._refresh_sidebar()
        self.query_one("#task-input", Input).focus()
        if self.initial_task:
            self.call_after_refresh(self.start_task, self.initial_task, self.resume)

    # 用户按 Enter 时触发：
    # 获取输入内容
    # 如果是 /new → 创建新会话
    # 否则 → 启动新任务
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """
        4. 用户交互回调
        用户输入完按回车触发：
            /new指令：调用start_new_session()创建全新会话，换新工作目录；
            普通文本任务：调用start_task(task, None)启动 agent。
        :param event:
        :return:
        """
        if event.input.id != "task-input":
            return
        task = event.value.strip()
        if not task or self.running:
            return
        event.input.value = ""
        if task == "/new":
            self.start_new_session()
            return
        self.start_task(task, None)

    # 收到 Agent 事件时触发：
    # 调用 _handle_event() 处理事件
    def on_agent_event_message(self, message: AgentEventMessage) -> None:
        self._handle_event(message.event)

    # 任务完成时触发：
    # 设置 running = False
    # 启用输入框
    # 更新状态为 "ready"
    # 刷新侧边栏
    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self.running = False
        self.resume = None
        self.query_one("#task-input", Input).disabled = False
        self.query_one("#task-input", Input).focus()
        self.query_one("#status", Static).update(f"{message.status}; ready for next task")
        self._refresh_sidebar()

    # 需要用户审批时触发：
    # 弹出审批对话框（ApprovalModal）
    # 等待用户选择"批准"或"拒绝"
    def on_approval_requested_message(self, message: ApprovalRequestedMessage) -> None:
        workspace = self.latest_workspace or str(self.workspace or "")
        self.push_screen(ApprovalModal(message.gate.request, workspace), self._resolve_approval(message.gate))

    # Ctrl+C 快捷键：
    def action_cancel_or_quit(self) -> None:
        if self.running:
            self.notify("A run is active. Press Ctrl+Q to quit and let checkpoint handle recovery.", severity="warning")
            return
        self.exit()

    # Ctrl+L 快捷键：
    # 清空事件流
    # 重新显示欢迎信息
    def action_clear_events(self) -> None:
        self.query_one("#events", VerticalScroll).remove_children()
        self._write_welcome()

    # 启动一个新任务：
    # 检查是否已在运行
    # 更新状态（running = True）
    # 清空待办列表
    # 禁用输入框
    # 在事件流中写入"任务开始"
    # 在后台线程中运行 _run_stream()
    def start_task(self, task: str, resume: Path | None = None) -> None:
        if self.running:
            self.notify("MokioClaw is already running a task.", severity="warning")
            return
        self.running = True
        self.run_count += 1
        self.todos = []
        self.failed_tool_count = 0
        self.tool_count = 0
        self.query_one("#task-input", Input).disabled = True
        self.query_one("#status", Static).update("running")
        self._refresh_sidebar()
        # UI 写入 “用户任务开始” 卡片；
        self._write_run_start(task, resume)
        # run_worker(..., thread=True)：新开后台线程执行_run_stream。,✨run_worker是 Textual 内置 API，开启后台工作线程，不会阻塞 UI。
        self.run_worker(lambda: self._run_stream(task, resume), thread=True, exclusive=False,
                        name=f"mokioclaw-run-{self.run_count}")

    # 在后台线程中执行 Agent（核心逻辑）：
    # 调用 stream_session_events() 获取事件流
    # 每个事件通过 post_message() 发送到主线程
    # 捕获异常（KeyboardInterrupt、普通异常）
    # 最后发送 RunFinishedMessage
    def _run_stream(self, task: str, resume: Path | None) -> None:
        """
        ⚠️这个函数跑在子线程，不能直接修改任何 UI 控件！

        :param task:
        :param resume:
        :return:
        """
        status = "finished"
        try:
            approval_handler = self._approval_handler if self.approval_mode == "inline" else None
            # 调用stream_session_events()拿到 agent 事件生成器；
            for event in self.stream_factory(
                    task,
                    session_workspace=self.session_workspace,
                    max_attempts=self.max_attempts,
                    approval_mode=self.approval_mode,
                    approval_handler=approval_handler,
                    checkpoint_mode=self.checkpoint_mode,
                    resume_workspace=resume,
                    trace_mode=self.trace_mode,
            ):
                # 每拿到一个 event，通过消息投递给主线程；
                self.call_from_thread(self.post_message, AgentEventMessage(event))
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception as exc:
            status = "failed"
            error_event = {"type": "custom_event",
                           "event": {"type": "tui_error", "error": f"{type(exc).__name__}: {exc}"}}
            self.call_from_thread(self.post_message, AgentEventMessage(error_event))
        finally:
            self.call_from_thread(self.post_message, RunFinishedMessage(status))


    def _approval_handler(self, request: ApprovalRequest) -> ApprovalDecision:
        """
        当 agent 要执行高危 shell 命令，会调用这个 handler（运行在后台线程）：

        ApprovalGate是一个同步阻塞工具：子线程在这里卡住等待用户点击弹窗；
        往主线程发送弹窗请求消息；
        .wait()阻塞子线程，直到 UI 用户点击批准 / 拒绝，调用gate.resolve(result)解除阻塞，返回决定给 agent
        :param request:
        :return:
        """
        gate = ApprovalGate(request)
        self.call_from_thread(self.post_message, ApprovalRequestedMessage(gate))
        # 关键点：后台线程卡住等待，UI 线程依然可以操作界面。
        return gate.wait()

    # 审批回调（用户点击后执行）：
    # 用户选择后，调用 gate.resolve(approved) 解除阻塞
    # 刷新侧边栏
    def _resolve_approval(self, gate: ApprovalGate) -> Callable[[bool | None], None]:
        def resolve(result: bool | None) -> None:
            approved = bool(result)
            gate.resolve(approved)
            self._refresh_sidebar()

        return resolve

    # 处理一个 Agent 事件：
    # 更新内部状态（_update_state_from_event）
    # 如果该事件应该隐藏 → 只刷新侧边栏，不显示
    # 否则 → 生成摘要并写入事件流
    def _handle_event(self, event: dict[str, Any]) -> None:
        # _update_state_from_event(event)：更新内存状态（带锁保护），从 event 里面提取todos、工具计数、checkpoint 路径、session_id 等；
        self._update_state_from_event(event)
        if self._should_hide_event(event):
            self._refresh_sidebar()
            return
        summary = summarize_event(event)
        self._write_summary(summary)
        # _refresh_sidebar()：把内存里的会话状态，组装成表格，刷新右侧面板。
        self._refresh_sidebar()

    # 从事件中提取信息更新状态：
    # workspace → 更新工作区路径
    # graph_event → 遍历 payload，调用 _update_from_payload
    # custom_event → 调用 _update_from_payload
    def _update_state_from_event(self, event: dict[str, Any]) -> None:
        with self._state_lock:
            if event.get("type") == "workspace":
                self.latest_workspace = str(event.get("path", ""))
                self.session_workspace = Path(self.latest_workspace)
                return
            payload = event.get("event")
            if event.get("type") == "graph_event" and isinstance(payload, dict):
                for update in payload.values():
                    if isinstance(update, dict):
                        self._update_from_payload(update)
            elif event.get("type") == "custom_event" and isinstance(payload, dict):
                self._update_from_payload(payload)

    # 从 payload 中提取具体信息：
    # todos → 更新待办列表
    # tool_call → 工具调用计数 +1
    # tool_result → 记录成功/失败，记录审批次数
    # checkpoint_saved → 更新检查点路径
    # trace_summary → 更新追踪目录
    # session_started → 更新会话 ID 和轮次
    def _update_from_payload(self, payload: dict[str, Any]) -> None:
        if isinstance(payload.get("todos"), list):
            self.todos = payload["todos"]
        if payload.get("type") == "tool_call":
            self.tool_count += 1
        if payload.get("type") == "tool_result":
            result = payload.get("result")
            if isinstance(result, dict):
                if result.get("ok") is False:
                    self.failed_tool_count += 1
                if result.get("requires_approval"):
                    self.approval_count += 1
        if payload.get("type") == "checkpoint_saved":
            self.latest_checkpoint = str(payload.get("path", ""))
        if payload.get("type") == "trace_summary":
            self.latest_trace = str(payload.get("trace_dir", ""))
        if payload.get("type") == "session_started":
            self.session_id = str(payload.get("session_id", ""))
            self.session_turn = int(payload.get("turn_index", 0) or 0)
            self.latest_workspace = str(payload.get("workspace", self.latest_workspace))
        if payload.get("type") == "session_turn_started":
            self.session_turn = int(payload.get("turn", self.session_turn) or self.session_turn)
        if payload.get("type") == "session_turn_saved":
            self.session_turn = int(payload.get("turn", self.session_turn) or self.session_turn)
            self.last_route = str(payload.get("route", self.last_route))

    # 写入欢迎信息到事件流。
    def _write_welcome(self) -> None:
        self._mount_event_card(
            "MokioClaw",
            "Ask for a quick answer or coding work. Use /new to open a fresh workspace.",
            category="info",
            collapsed=True,
            detail="Persistent TUI sessions keep one workspace across turns. Workflow turns still use approval, checkpoint, trace, and layered memory.",
        )

    # 写入"任务开始"信息到事件流：
    # 显示任务内容（截断到 500 字符）
    # 显示工作区或恢复路径
    def _write_run_start(self, task: str, resume: Path | None) -> None:
        mode = f"resume: {resume}" if resume is not None else f"workspace: {self.session_workspace}"
        self._mount_event_card(
            f"You · turn {self.run_count}",
            shorten(task, 500),
            category="user",
            collapsed=False,
            detail=mode,
        )

    # 写入事件摘要到事件流：
    # 根据摘要类型决定颜色、折叠状态
    # 调用 _mount_event_card 实际渲染
    def _write_summary(self, summary: EventSummary) -> None:
        self._mount_event_card(
            summary.title,
            self._compact_body(summary),
            category=self._event_category(summary),
            collapsed=self._should_collapse(summary),
            detail=summary.body,
        )

    # 刷新右侧状态面板：
    # 收集所有状态信息（状态、轮次、会话ID、工作区、检查点、追踪、工具统计、待办）
    # 用 Table 表格显示
    # 更新到 #side-state 组件
    def _refresh_sidebar(self) -> None:
        """

        :return:
        """
        status = "running" if self.running else "ready"
        workspace = shorten(self.latest_workspace or str(self.session_workspace), 80)
        checkpoint = shorten(self.latest_checkpoint or "(waiting)", 80)
        trace = shorten(self.latest_trace or "(waiting)", 80)
        tools = f"{self.tool_count} total / {self.failed_tool_count} failed"
        approvals = str(self.approval_count)
        todos = self._todo_sidebar_text()
        self.sidebar_text = "\n".join(
            [
                f"status {status}",
                f"turns {self.run_count}",
                f"session {self.session_id}",
                f"route {self.last_route or '(none)'}",
                f"workspace {workspace}",
                f"checkpoint {checkpoint}",
                f"trace {trace}",
                f"tools {tools}",
                f"approvals {approvals}",
                f"todos {todos}",
            ]
        )
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        table.add_row("status", status)
        table.add_row("turns", str(self.run_count))
        table.add_row("session", shorten(self.session_id or "(starting)", 24))
        table.add_row("route", self.last_route or "(none)")
        table.add_row("workspace", workspace)
        table.add_row("checkpoint", checkpoint)
        table.add_row("trace", trace)
        table.add_row("tools", tools)
        table.add_row("approvals", approvals)
        table.add_row("todos", todos)
        self.query_one("#side-state", Static).update(table)

    # def _todo_sidebar_text(self)
    # 生成待办事项的文本：
    # 统计各状态数量（pending/in_progress/done）
    # 如果有进行中的任务，显示其内容
    def _todo_sidebar_text(self) -> str:
        if not self.todos:
            return "(none yet)"
        counts: dict[str, int] = {}
        for todo in self.todos:
            status = str(todo.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        current = next((todo for todo in self.todos if todo.get("status") == "in_progress"), None)
        count_text = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
        if current:
            return f"{count_text}\n{shorten(current.get('content', current.get('description', '')), 120)}"
        return count_text

    # 染一个事件卡片到事件流区域：
    # 左侧有颜色标记（运行中=黄色，成功=绿色，错误=红色）
    # 可折叠（Collapsible）或展开（Vertical）
    # 滚动到底部
    def _mount_event_card(
            self,
            title: str,
            body: str,
            *,
            category: str = "info",
            collapsed: bool = True,
            detail: str | None = None,
    ) -> None:
        events = self.query_one("#events", VerticalScroll)
        title_text = f"{self._category_marker(category)} {title}"
        summary = Static(Text(body or " ", style=self._category_style(category)), classes="event-summary")
        detail_text = detail if detail is not None else body
        if collapsed:
            card = Collapsible(
                summary,
                Static(self._detail_renderable(detail_text), classes="detail"),
                title=title_text,
                collapsed=True,
                classes=f"event-card {self._category_class(category)}",
            )
        else:
            card = Vertical(
                Static(Text(title_text, style=f"bold {self._category_style(category)}")),
                summary,
                classes=f"event-card {self._category_class(category)}",
            )
            card.styles.height = "auto"
        events.mount(card)
        events.scroll_end(animate=False)

    def _compact_body(self, summary: EventSummary) -> str:
        title = summary.title
        body = summary.body or ""
        if summary.category == "session":
            return self._first_matching_line(body, ("route:", "turn:", "workspace:", "session:")) or shorten(body, 140)
        if summary.category == "intent":
            route = self._line_value(body, "route")
            reason = self._line_value(body, "reason")
            return f"route {route or 'workflow'}" + (f" · {shorten(reason, 90)}" if reason else "")
        if summary.category == "chat":
            return shorten(body.split("\nmode:")[0], 2400)
        if summary.category == "plan":
            todos = self._line_value(body, "todos")
            first = body.splitlines()[0] if body.splitlines() else title
            return shorten(first + (f" · todos {todos}" if todos else ""), 180)
        if summary.category == "tool_call":
            return shorten(body, 160)
        if summary.category == "tool_result":
            ok = self._line_value(body, "ok")
            path = self._line_value(body, "path") or self._line_value(body, "stdout_path")
            pieces = [f"ok={ok}" if ok else "tool result"]
            if path:
                pieces.append(path)
            return shorten(" · ".join(pieces), 180)
        if summary.category == "handoff":
            return shorten(body, 180)
        if summary.category == "memory":
            return "memory snapshot updated"
        if summary.category == "context":
            return self._first_matching_line(body, ("tokens:", "compress:", "next:")) or shorten(body, 160)
        if summary.category == "checkpoint":
            status = self._line_value(body, "status") or self._line_value(body, "mode")
            return f"checkpoint {status}" if status else "checkpoint updated"
        if summary.category == "trace":
            status = self._line_value(body, "status")
            tools = self._line_value(body, "tools")
            return " · ".join(
                part for part in [f"status {status}" if status else "", f"tools {tools}" if tools else ""] if part)
        if summary.category == "final":
            return shorten(body.splitlines()[0] if body.splitlines() else body, 220)
        if summary.category == "verifier":
            return shorten(body.splitlines()[0] if body.splitlines() else body, 180)
        return shorten(body, 180)

    # 压缩事件主体，针对不同类型提取关键信息：
    # session → 提取 route/turn/workspace
    # tool_result → 提取 ok/path
    # final → 只显示第一行
    # 默认 → 截断到 180 字符
    def _should_collapse(self, summary: EventSummary) -> bool:
        return summary.category not in {"chat", "final", "verifier"}

    # 判断事件类别：running、success、error、info、user。用于决定颜色和样式。
    def _event_category(self, summary: EventSummary) -> str:
        if summary.category in {"final", "trace"}:
            return "success"
        if summary.category in {"verifier", "tool_result"} and "FAIL" in summary.body:
            return "error"
        if summary.category in {"plan", "tool_call", "handoff", "context", "checkpoint"}:
            return "running"
        return "info"

    # 判断是否折叠：只有 chat、final、verifier 不折叠，其他都折叠。
    def _should_hide_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") == "workspace":
            return True
        payload = event.get("event")
        if event.get("type") == "graph_event" and isinstance(payload, dict):
            hidden_nodes = {"intent_router", "chat_responder"}
            return all(node in hidden_nodes for node in payload)
        if event.get("type") == "custom_event" and isinstance(payload, dict):
            return payload.get("type") in {"session_started", "session_turn_started", "memory_snapshot"}
        return False

    # 格式化详情内容：
    # 尝试解析 JSON → 用 Rich 的 Pretty 美化显示
    # 否则 → 显示纯文本
    def _detail_renderable(self, detail: str) -> Any:
        text = detail or "(no details)"
        if len(text) > 1600:
            text = text[:1597] + "..."
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return Text(text)
        return Pretty(parsed, max_depth=4)

    # 事件类别对应的标记符号
    def _category_marker(self, category: str) -> str:
        return {
            "running": "•",
            "success": "✓",
            "error": "!",
            "info": "·",
            "user": ">",
        }.get(category, "·")

    # 事件类别对应的 CSS 样式
    def _category_style(self, category: str) -> str:
        return {
            "running": "#f4bf75",
            "success": "#7fd68a",
            "error": "#ef6f6c",
            "info": "#7fd6c2",
            "user": "#f3ede3",
        }.get(category, "#d7d1c9")

    # 事件类别对应的 CSS 类名
    def _category_class(self, category: str) -> str:
        return {
            "running": "event-running",
            "success": "event-success",
            "error": "event-error",
            "info": "event-info",
            "user": "event-user",
        }.get(category, "event-info")

    # 提取事件主体中的指定键值对值
    def _line_value(self, body: str, key: str) -> str:
        prefix = f"{key}:"
        for line in body.splitlines():
            if line.strip().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    # 提取事件主体中的第一个匹配行，用于显示在事件卡片中。
    def _first_matching_line(self, body: str, prefixes: tuple[str, ...]) -> str:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefixes):
                return stripped
        return ""

    # 开始新会话
    def start_new_session(self) -> None:
        if self.running:
            self.notify("MokioClaw is already running a task.", severity="warning")
            return
        self.workspace = default_workspace()
        self.session_workspace = self.workspace
        self.resume = None
        self.latest_workspace = str(self.session_workspace)
        self.latest_checkpoint = ""
        self.latest_trace = ""
        self.session_id = ""
        self.session_turn = 0
        self.last_route = ""
        self.todos = []
        self.failed_tool_count = 0
        self.tool_count = 0
        self.approval_count = 0
        self._refresh_sidebar()
        self._mount_event_card(
            "New Session",
            str(self.session_workspace),
            category="info",
            collapsed=False,
        )
