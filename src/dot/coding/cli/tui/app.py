"""
dot.coding.cli.tui.app — TUI 交互应用（Textual）

架构对齐 pi_src/tau 的 TuiState + TuiEventAdapter + Textual 渲染：
- state/adapter 纯数据层（不依赖 UI）
- App 持有 harness，run_worker(exclusive) 消费 AgentEvent 流
- Esc = 中断当前回合（不退出），Ctrl+D = 退出
- 权限审批走 ModalScreen（异步 handler）
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Footer, Markdown, Static, TextArea

from dot.agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from dot.ai.events import TextDeltaEvent, ThinkingDeltaEvent
from dot.ai.types import AssistantMessage, UserMessage
from dot.coding.commands import get_command_registry
from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode

from .adapter import TuiEventAdapter
from .autocomplete import build_completion_state, render_completions
from .state import TuiState
from .widgets import StatusBar, TranscriptView


# ============================================================
# 输入区
# ============================================================

class PromptInput(TextArea):
    """多行输入框：Enter 提交，Alt+Enter 换行，补全激活时 Tab/上下 导航"""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("alt+enter", "newline", "Newline", show=False),
        Binding("tab", "accept_completion", "Accept", show=False),
        Binding("down", "completion_next", show=False),
        Binding("up", "completion_prev", show=False),
    ]

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_accept_completion(self) -> None:
        completion = self.app._completion
        if completion is not None and completion.active:
            self.app.action_accept_completion()
        else:
            self.app.action_cycle_mode()

    def action_completion_next(self) -> None:
        self.app.action_completion_next()

    def action_completion_prev(self) -> None:
        self.app.action_completion_prev()


# ============================================================
# 权限审批弹窗
# ============================================================

class PermissionModal(ModalScreen[bool]):
    """工具权限审批：显示工具名/来源/原因/参数，y 批准，n / Esc 拒绝"""

    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n", "deny", "Deny"),
        Binding("escape", "deny", "Deny", show=False),
    ]

    DEFAULT_CSS = """
    PermissionDialog { width: 60; height: auto; border: thick $warning;
                       background: $panel; padding: 1 2; }
    """

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__()
        self._info = info

    def compose(self) -> ComposeResult:
        info = self._info
        args_text = str(info.get("args", {}))[:400]
        yield Vertical(
            Static(
                f"[bold yellow]Permission required[/bold yellow]\n\n"
                f"tool:   {info.get('tool_name', '')}\n"
                f"source: {info.get('source', '')}\n"
                f"reason: {info.get('reason', '')}\n"
                f"args:   {args_text}\n\n"
                f"[dim]y = approve · n / esc = deny[/dim]",
            ),
            classes="PermissionDialog",
        )

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


# ============================================================
# 主应用
# ============================================================

class DotTUIApp(App[None]):
    """dot agent TUI — Textual 全屏应用"""

    TITLE = "dot agent"
    CSS = """
    #main { height: 100%; }
    #transcript { height: 1fr; }
    #input-area { height: auto; max-height: 12; border-top: solid $primary; padding: 0 1; }
    #autocomplete { height: auto; max-height: 10; display: none;
                    background: $panel; margin: 0 1; }
    #autocomplete.visible { display: block; }
    #prompt { height: auto; max-height: 8; border: none; background: transparent; }
    #prompt.running { border-left: thick $warning; }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit", "Quit"),
        Binding("ctrl+c", "clear_prompt", "Clear input", show=False),
        Binding("ctrl+o", "toggle_tool_results", "Toggle tool results"),
        Binding("ctrl+t", "toggle_thinking", "Toggle thinking", show=False),
        Binding("escape", "cancel_agent", "Cancel"),
    ]

    def __init__(self, host: CodingHost, *, system: str = "") -> None:
        super().__init__()
        self.host = host
        self.commands = get_command_registry()
        self.commands.set_host(host)
        self.state = TuiState()
        self.adapter = TuiEventAdapter(self.state)
        self._harness = host.create_harness(
            system=system or "You are a helpful coding assistant.",
        )
        self._run_id = 0
        self._completion: Any = None

    # ============================================================
    # 布局
    # ============================================================

    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            yield TranscriptView()
            with Vertical(id="input-area"):
                yield Static(id="autocomplete")
                yield PromptInput(id="prompt", soft_wrap=True)
            yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt", PromptInput).focus()
        self.set_interval(0.15, self._tick_status)
        self._refresh_status()

    # ============================================================
    # 提交 / 补全
    # ============================================================

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.text = ""
        self._set_completion(None)
        if text.startswith("/"):
            await self._render_slash_result(self.commands.execute(text))
            return
        self._run_agent_turn(text)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "prompt":
            self._set_completion(build_completion_state(event.text_area.text, self.commands))

    def _set_completion(self, completion: Any) -> None:
        self._completion = completion
        panel = self.query_one("#autocomplete", Static)
        panel.update(render_completions(completion) if completion else "")
        panel.set_class(bool(completion and completion.active), "visible")

    def action_accept_completion(self) -> None:
        completion = self._completion
        if not completion or not completion.active:
            return
        current = completion.current
        if current is None:
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.text = current[0]
        prompt.move_cursor(prompt.document.end)
        self._set_completion(build_completion_state(prompt.text, self.commands))

    def action_completion_next(self) -> None:
        if self._completion and self._completion.active:
            self._completion.move_next()
            self._refresh_completion_panel()

    def action_completion_prev(self) -> None:
        if self._completion and self._completion.active:
            self._completion.move_previous()
            self._refresh_completion_panel()

    def _refresh_completion_panel(self) -> None:
        panel = self.query_one("#autocomplete", Static)
        panel.update(render_completions(self._completion))
        panel.set_class(bool(self._completion and self._completion.active), "visible")

    # ============================================================
    # Agent 回合（Textual worker，exclusive）
    # ============================================================

    @work(exclusive=True, group="prompt")
    async def _run_agent_turn(self, text: str) -> None:
        self._run_id += 1
        run_id = self._run_id
        self.state.running = True
        self._set_running_visual(True)

        self.host.permission.set_approval_handler(self._make_approval_handler())

        transcript = self.query_one(TranscriptView)
        try:
            async for event in self._harness.prompt(text):
                if run_id != self._run_id:
                    return  # 陈旧事件：已被更新的一次回合取代
                self.adapter.apply(event)
                await self._apply_event_to_transcript(event)
            self.host.end_turn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await transcript.add_error(str(exc))
        finally:
            if run_id == self._run_id:
                self.state.running = False
                self.state.assistant_buffer = ""
                await transcript.finish_streaming()
                self._set_running_visual(False)
                self._refresh_status()

    async def _apply_event_to_transcript(self, event) -> None:
        """事件 → 增量渲染（对齐 tau：增量为主，终态校正）"""
        transcript = self.query_one(TranscriptView)

        if isinstance(event, MessageStartEvent):
            if isinstance(event.message, AssistantMessage):
                await transcript.start_streaming()

        elif isinstance(event, MessageUpdateEvent):
            nested = event.provider_event
            if isinstance(nested, TextDeltaEvent):
                await transcript.stream_delta(nested.delta)
            # thinking 流式 v1 不逐字渲染（Ctrl+T 开启时由 MessageEnd 终态补一条）

        elif isinstance(event, MessageEndEvent):
            msg = event.message
            if isinstance(msg, UserMessage):
                await transcript.add_user(msg.text)
            elif isinstance(msg, AssistantMessage):
                if msg.stop_reason in {"error", "aborted"}:
                    await transcript.finish_streaming(None)
                    await transcript.add_error(msg.error_message or "error")
                else:
                    await transcript.finish_streaming(msg.text)
                if self.state.show_thinking:
                    thinking = [i for i in self.state.items if i.role == "thinking"]
                    if thinking and thinking[-1].text:
                        await transcript.add_thinking(thinking[-1].text)

        elif isinstance(event, ToolExecutionStartEvent):
            item = self.state.find_tool_item(event.tool_call_id)
            if item is not None:
                await transcript.add_tool(item)

        elif isinstance(event, (ToolExecutionUpdateEvent, ToolExecutionEndEvent)):
            item = self.state.find_tool_item(event.tool_call_id)
            if item is not None:
                transcript.update_tool(item)

    def _set_running_visual(self, running: bool) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.set_class(running, "running")
        self._refresh_status()

    def _tick_status(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        bar = self.query_one(StatusBar)
        bar.render_state(
            self.state,
            mode_label=self.host.mode.label,
            workspace=self.host.workspace,
            session_id=self.host.session_id,
        )

    async def _render_slash_result(self, result) -> None:
        transcript = self.query_one(TranscriptView)
        if result.kind == "prompt":
            # skill 即命令：skill 内容 + 任务作为一轮对话送入 agent
            self._run_agent_turn(result.text)
            return
        if result.kind == "message":
            await transcript.add_status(result.text)
        elif result.kind == "toast":
            style = {"error": "bold red", "warn": "bold yellow"}.get(result.level, "bold cyan")
            await transcript.add_status(f"[{style}]{result.text}[/{style}]")
        elif result.kind == "clear_screen":
            await transcript.remove_children()
            self.state.clear()
        elif result.kind == "quit":
            self.exit()

    # ============================================================
    # 动作
    # ============================================================

    def action_clear_prompt(self) -> None:
        self.query_one("#prompt", PromptInput).text = ""

    async def action_cancel_agent(self) -> None:
        """Esc：中断当前 agent 回合（不退出）"""
        if not self.state.running:
            return
        self._run_id += 1  # 使陈旧事件失效
        self._harness.cancel()
        for worker in list(self.workers):
            if worker.group == "prompt":
                worker.cancel()
        self.state.running = False
        self.state.assistant_buffer = ""
        transcript = self.query_one(TranscriptView)
        await transcript.finish_streaming()
        await transcript.add_status("· interrupted")
        self._set_running_visual(False)

    def action_cycle_mode(self) -> None:
        """Tab：在 auto → plan → edit → auto 间循环切换模式"""
        order = [AgentMode.AUTO, AgentMode.PLAN, AgentMode.EDIT]
        current = self.host.mode
        nxt = order[(order.index(current) + 1) % len(order)]
        self.host.set_mode(nxt)
        self._refresh_status()
        self.run_worker(
            self.query_one(TranscriptView).add_status(f"· mode: {nxt.label}"),
            exclusive=False,
        )

    def action_toggle_tool_results(self) -> None:
        self.state.show_tool_results = not self.state.show_tool_results
        transcript = self.query_one(TranscriptView)
        for widget in transcript._tool_widgets.values():
            widget.set_expanded(self.state.show_tool_results)

    def action_toggle_thinking(self) -> None:
        self.state.show_thinking = not self.state.show_thinking

    # ============================================================
    # 权限审批（异步 Modal）
    # ============================================================

    def _make_approval_handler(self):
        async def _approve(info: dict) -> bool:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()

            def _push() -> None:
                if fut.done():
                    return
                self.push_screen(
                    PermissionModal(info),
                    callback=lambda approved: (
                        None if fut.done() else fut.set_result(bool(approved))
                    ),
                )

            self.call_later(_push)
            return await fut

        return _approve


# ============================================================
# 兼容入口
# ============================================================

class DotTUI:
    """保持旧的 DotTUI().run() 用法"""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        mode: AgentMode = AgentMode.AUTO,
        session_id: str | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.mode = mode
        self.host = CodingHost(workspace=self.workspace, mode=self.mode)
        if session_id:
            if not self.host.resume_session(session_id):
                print(f"session not found: {session_id}, starting new session")
            else:
                print(f"resumed {session_id} ({len(self.host.session.messages)} messages)")

    def run(self) -> None:
        DotTUIApp(self.host).run()
