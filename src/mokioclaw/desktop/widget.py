"""
桌面宠物 TUI Widget

在 Textual TUI 中显示 agent 状态的小组件：
- 状态指示器（idle/thinking/tool_call/error/completed）
- 当前任务显示
- 最近操作时间线
- 快捷键提示
"""
from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Static


class PetStatusWidget(Static):
    """Agent 状态指示器"""

    DEFAULT_CSS = """
    PetStatusWidget {
        height: auto;
        border: solid $primary;
        padding: 1;
        background: $surface;
    }
    """

    STATUS_EMOJI = {
        "idle": "😴",
        "thinking": "🤔",
        "tool_call": "⚡",
        "waiting_approval": "⏳",
        "error": "❌",
        "completed": "✅",
    }

    def render_status(self, status_snapshot: dict[str, Any]) -> str:
        agent_status = status_snapshot.get("agent_status", "idle")
        emoji = self.STATUS_EMOJI.get(agent_status, "🤖")
        task = status_snapshot.get("current_task", "")[:60]
        last_action = status_snapshot.get("last_action", "")
        uptime = status_snapshot.get("uptime_seconds", 0)
        uptime_str = _format_uptime(uptime)

        lines = [
            f"[bold cyan]dot_agent[/bold cyan] [dim]pet[/dim]",
            f"  {emoji} [bold]{agent_status}[/bold]",
        ]
        if task:
            lines.append(f"  📋 {task}")
        if last_action:
            lines.append(f"  🔧 {last_action[:40]}")
        lines.append(f"  ⏱️ {uptime_str}")
        return "\n".join(lines)

    def watch_status_snapshot(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        """状态变化时自动更新显示"""
        self.update(self.render_status(new))


class PetTimelineWidget(Static):
    """最近操作时间线"""

    DEFAULT_CSS = """
    PetTimelineWidget {
        height: auto;
        border: solid $primary;
        padding: 1;
        background: $surface;
        max-height: 12;
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events: list[dict[str, Any]] = []
        self._max_events = 20

    def add_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        self._render()

    def _render(self) -> None:
        lines = ["[bold cyan]Timeline[/bold cyan]"]
        for evt in reversed(self._events[-8:]):
            evt_type = evt.get("type", "?")
            node = evt.get("node", "")
            ts = evt.get("timestamp", "")[5:19]  # MM-DD HH:MM:SS
            if evt_type == "tool_call":
                name = evt.get("name", "?")
                lines.append(f"  [dim]{ts}[/dim] 🔧 {node}.{name}")
            elif evt_type == "tool_result":
                result = evt.get("result", {})
                ok = result.get("ok") if isinstance(result, dict) else None
                icon = "✅" if ok else "❌"
                lines.append(f"  [dim]{ts}[/dim] {icon} {node}")
            elif evt_type == "handoff":
                fr = evt.get("from", "?")
                to = evt.get("to", "?")
                lines.append(f"  [dim]{ts}[/dim] 🔄 {fr}→{to}")
            elif evt_type == "final":
                lines.append(f"  [dim]{ts}[/dim] 🎉 完成")
            elif evt_type == "context_compression":
                b = evt.get("before_tokens", 0)
                a = evt.get("after_tokens", 0)
                lines.append(f"  [dim]{ts}[/dim] 📦 {b}→{a} tokens")
            else:
                lines.append(f"  [dim]{ts}[/dim] {evt_type}")

        self.update("\n".join(lines))


class PetHotkeyHint(Static):
    """热键提示"""

    DEFAULT_CSS = """
    PetHotkeyHint {
        height: auto;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }
    """

    def render(self) -> str:
        return " [dim]F8 / Alt+`[/dim] [dim]唤起 TUI[/dim] | [dim]Ctrl+C[/dim] [dim]退出[/dim] "


class DesktopPetView(Container):
    """桌面宠物主视图（可嵌入 TUI Sidebar）"""

    DEFAULT_CSS = """
    DesktopPetView {
        layout: vertical;
        height: auto;
        padding: 1;
        background: $surface;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._status_snapshot: dict[str, Any] = {
            "agent_status": "idle",
            "current_task": "",
            "last_action": "",
            "uptime_seconds": 0.0,
        }
        self._timeline: PetTimelineWidget | None = None
        self._status_widget: PetStatusWidget | None = None

    def compose(self) -> ComposeResult:
        yield PetStatusWidget()
        yield PetTimelineWidget()
        yield PetHotkeyHint()

    def on_mount(self) -> None:
        self._status_widget = self.query_one(PetStatusWidget)
        self._timeline = self.query_one(PetTimelineWidget)

    def update_status(self, snapshot: dict[str, Any]) -> None:
        """更新状态快照"""
        self._status_snapshot = snapshot
        if self._status_widget is not None:
            self._status_widget.update(
                self._status_widget.render_status(snapshot)
            )

    def add_event(self, event: dict[str, Any]) -> None:
        """添加事件到时间线"""
        if self._timeline is not None:
            self._timeline.add_event(event)


def _format_uptime(seconds: float) -> str:
    """格式化运行时长"""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"
