# MokioClaw CLI 模块

## 概览

CLI 模块提供两种交互方式：

```
mokioclaw "你的任务描述"     # Rich 终端输出（单次执行）
mokioclaw tui               # Textual TUI 界面（多轮会话）
```

## 架构

```
cli/
├── app.py              # Typer 入口，定义命令和参数
├── formatter.py        # Rich 格式化器，将事件渲染为终端输出
├── event_summary.py    # 事件摘要器，将事件转为结构化文本（供 TUI 使用）
├── tui/
│   ├── app.py          # Textual TUI 主应用
│   ├── approval.py     # 审批弹窗（模态对话框）
│   └── logo.py         # PNG logo 渲染为终端字符画
└── README.md           # 本文件
```

### 数据流

```
用户输入
  │
  ▼
app.py (Typer 解析参数)
  │
  ├─ Rich 模式 ──► stream_agent_events() ──► formatter.py ──► 终端
  │
  └─ TUI 模式 ──► tui/app.py ──► stream_session_events()
                    │                    │
                    │                    ▼
                    │              event_summary.py ──► TUI 组件
                    │
                    └─► approval.py (弹窗审批)
```

## Typer 框架速查

[Typer](https://typer.tiangolo.com/) 是基于 Click 的 CLI 框架，用类型注解自动生成命令行参数。

### 核心概念

```python
import typer
from typing import Annotated

app = typer.Typer()

# @app.callback(invoke_without_command=True) 表示"默认命令"
# 当用户运行 mokioclaw "task" 而不是 mokioclaw tui 时触发
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,                          # Typer 上下文，获取子命令信息
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="...")
    ] = None,                                     # Annotated[type, typer.Option(...)] 定义选项
    max_attempts: Annotated[int, typer.Option(...)] = 3,
):
    # ctx.invoked_subcommand 不为 None 时，说明用户调了子命令（如 tui）
    # 此时 callback 只做参数解析，不执行主逻辑
    if ctx.invoked_subcommand is not None:
        return
    # ... 主逻辑

# @app.command("tui") 定义子命令，运行方式：mokioclaw tui
@app.command("tui")
def tui(
    task: Annotated[str | None, typer.Argument(help="...")] = None,  # typer.Argument = 位置参数
):
    """子命令的 docstring 会显示在 --help 中"""
    ...
```

### MokioClawGroup 自定义解析

默认 Typer 行为：`mokioclaw "hello"` 会报错，因为 "hello" 不是已知子命令。

`MokioClawGroup` 重写了 `parse_args`：
- 如果参数是已知子命令名（如 `tui`）或 `--help` → 交给子命令处理
- 如果参数以 `-` 开头 → 当作选项处理
- 其余所有内容 → 收集为 `task_arg` 存入 `ctx.obj`

这样 `mokioclaw "写个贪吃蛇"` 和 `mokioclaw --workspace ./out "写个贪吃蛇"` 都能工作。

## Rich 格式化器 (formatter.py)

[Rich](https://rich.readthedocs.io/) 是 Python 的富文本终端库。

### 核心组件

- `Console` — 终端输出入口，替代 `print()`
- `Panel` — 带标题和边框的面板
- `Table` — 表格
- `Text` — 带样式的文本

### 事件渲染流程

```python
# formatter.py 的入口函数
def print_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type == "custom_event":      # 自定义事件（意图决策、工具调用等）
        print_custom_event(event["event"])
    elif event_type == "graph_event":     # LangGraph 图更新（planner/verifier 节点变化）
        print_graph_event(event["event"])
    else:                                  # 其他事件（workspace 路径等）
        console.print(_shorten(event))
```

事件类型层级：
```
event["type"]
├── "workspace"        → 显示工作区路径
├── "custom_event"     → event["event"]["type"] 再细分
│   ├── "intent_decision"      → 意图路由结果
│   ├── "chat_response"        → 聊天回复
│   ├── "plan_snapshot"        → 计划快照（带 todo 表格）
│   ├── "tool_call"            → 工具调用（显示参数）
│   ├── "tool_result"          → 工具结果（成功绿色/失败红色）
│   ├── "handoff" / "handoff_result" → 智能体交接
│   ├── "search_results"       → 搜索结果（来源表格）
│   ├── "memory_snapshot"      → 三层记忆快照
│   ├── "context_monitor"      → 上下文 token 监控
│   ├── "checkpoint_saved"     → 检查点保存
│   └── "trace_summary"        → 追踪摘要
└── "graph_event"      → LangGraph 节点更新
    ├── "planner"      → 计划更新
    ├── "codeAgent"    → 代码智能体摘要
    ├── "verifier"     → 校验结果（PASS/FAIL 表格）
    └── "final"        → 最终结果
```

## 事件摘要器 (event_summary.py)

`event_summary.py` 和 `formatter.py` 处理相同的事件类型，但输出格式不同：

| | formatter.py | event_summary.py |
|---|---|---|
| 输出目标 | 终端（Rich 渲染） | TUI 组件（纯文本） |
| 返回值 | `None`（直接打印） | `EventSummary` 数据类 |
| 使用场景 | `mokioclaw "task"` | `mokioclaw tui` |

`EventSummary` 结构：
```python
@dataclass(frozen=True)
class EventSummary:
    title: str        # 标题，如 "Planner"、"Verifier"
    body: str         # 正文内容
    category: str     # 分类，用于 TUI 样式：event/plan/tool_call/verifier 等
    style: str        # Rich 样式名：cyan/green/red/yellow 等
```

## Textual TUI (tui/app.py)

[Textual](https://textual.textualize.io/) 是 Rich 作者开发的 TUI 框架，支持组件化、CSS 样式、事件驱动。

### 核心概念

```python
from textual.app import App, ComposeResult
from textual.widgets import Static, Input, Footer, Header

class MokioClawTuiApp(App[None]):
    # CSS 样式（类似网页 CSS，但用 Textual 的选择器）
    CSS = """
    Screen { background: #101113; }
    #input-bar { height: 3; dock: bottom; }
    """

    # 键盘绑定
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
    ]

    # compose() 类似 React 的 render()，声明组件树
    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(...)   # 可滚动的事件列表
        yield Input(placeholder="输入任务...")  # 底部输入框
        yield Footer()

    # Textual 的事件处理：on_<事件类型> 或 handle_<消息类型>
    def on_input_submitted(self, event: Input.Submitted) -> None:
        task = event.value
        # 启动后台线程执行 agent
        self._start_agent(task)
```

### 线程模型

TUI 运行在 Textual 的事件循环中。Agent 执行是阻塞的，所以放在独立线程：

```
主线程（Textual 事件循环）
  │
  ├─ 用户输入 → _start_agent()
  │                │
  │                ▼
  ├─         工作线程：stream_session_events()
  │                │
  │                ├─ 每个事件 → post_message(AgentEventMessage)
  │                │                    │
  │                │                    ▼
  │                │              主线程 handle_agent_event_message()
  │                │              → 更新 TUI 组件
  │                │
  │                └─ 完成 → post_message(RunFinishedMessage)
  │
  └─ 审批请求 → post_message(ApprovalRequestedMessage)
                    │
                    ▼
              弹出 ApprovalModal（模态对话框）
              用户按 y/n → resolve gate → 工作线程继续
```

### Message 机制

Textual 用 `Message` 子类在组件间通信：

```python
class AgentEventMessage(Message):
    """工作线程产生的 agent 事件，投递到主线程更新 UI"""
    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self.event = event

# 工作线程发送：
self.post_message(AgentEventMessage(event))

# 主线程接收：
def handle_agent_event_message(self, message: AgentEventMessage) -> None:
    self._render_event(message.event)
```

## 审批系统 (tui/approval.py)

当 BashTool 检测到危险命令时，需要人工审批。

### 流程

```
BashTool 检测到危险命令
  │
  ▼
创建 ApprovalRequest（包含命令、风险原因）
  │
  ├─ Rich 模式：_inline_approval_handler()
  │   → 终端打印 Panel，用 typer.prompt() 等用户输入 y/n
  │
  └─ TUI 模式：ApprovalGate + ApprovalModal
      → 工作线程创建 gate，post_message 投递到主线程
      → 主线程弹出 ModalScreen
      → 用户按 y/n/Enter/Esc
      → gate.resolve(approved)
      → gate.wait() 返回 ApprovalDecision
      → 工作线程继续执行
```

`ApprovalGate` 是线程间同步原语：
```python
@dataclass
class ApprovalGate:
    request: ApprovalRequest
    decision: ApprovalDecision | None = None
    _ready: Event  # threading.Event，用于阻塞等待

    def resolve(self, approved: bool) -> None:
        self.decision = ApprovalDecision(approved=approved, ...)
        self._ready.set()           # 唤醒等待线程

    def wait(self) -> ApprovalDecision:
        self._ready.wait()          # 阻塞直到 resolve() 被调用
        return self.decision
```

## logo.py

将 PNG 图片渲染为终端字符画。使用 Unicode 半块字符（▀▄）实现双倍垂直分辨率：

```
一个终端字符 = 上半像素(▀) + 下半像素(▄)
  │
  ▼
对图片每两行像素：
  - 上行有颜色 → 用 ▀ 的前景色
  - 下行有颜色 → 用 ▄ 的背景色
  - 两行都有 → ▀ 前景+背景
```

如果 PNG 加载失败，回退到 ASCII art 猫头 logo。
