"""
dot.coding.host — CodingHost（组装层 / 组合根）

组装 dot.ai + dot.agent + dot.coding 三层，提供统一的执行入口。
自身只做装配与委托，具体职责拆分在各子模块：

- 工具注册      ：coding.tools.*
- 链路追踪      ：coding.trace.controller.TraceController
- 上下文压缩    ：coding.compress.compactor.ContextCompactor
                  （AutoCompactor 实现 agent 层 CompactionGate，turn 边界自动触发）
- MCP 连接      ：coding.extensions.builtins.mcp.manager.McpConnector
- 会话持久化    ：coding.session.manager.SessionManager
- 权限          ：coding.permission.PermissionManager
- 扩展          ：coding.extensions.runtime.ExtensionRuntime
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dot.agent.events import AgentEvent
from dot.agent.harness import AgentHarness, AgentHarnessConfig
from dot.agent.tools import AgentTool
from dot.ai.catalog import ProviderCatalog
from dot.ai.providers.openai import OpenAIProvider
from dot.workflow import WorkflowEvent

from .compress.compactor import ContextCompactor
from .compress.auto_compact import AutoCompactor
from .extensions.builtins.mcp.manager import McpConnector
from .extensions.runtime import ExtensionRuntime
from .modes import AgentMode
from .permission import get_permission_manager
from .session import Session
from .session.manager import SessionManager
from .trace.controller import TraceController

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .workflow import HumanInterventionHandler


class CodingHost:
    """Coding Agent 主机组装（组合根，只装配与委托）

    一个 CodingHost = 一个工作空间 = Provider + Agent + Tools + Permission + Extensions + Session
    """

    def __init__(
            self,
            workspace: Path | None = None,
            *,
            mode: AgentMode = AgentMode.AUTO,
            extra_extension_dirs: list[Path] | None = None,
            auto_compact: bool = True,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.mode = mode
        self.auto_compact = auto_compact

        # 加载 Provider 配置
        # todo： 暂时实现env的配置，配置大模型参数，后面改为文件配置
        self.catalog = ProviderCatalog.load()
        self.provider = OpenAIProvider()

        # 权限管理
        self.permission = get_permission_manager()
        self.permission.load_project(self.workspace)

        # 扩展运行时
        self.extensions = ExtensionRuntime(
            workspace=self.workspace,
            extra_dirs=extra_extension_dirs,
        )
        self.extensions.set_runtime_dep("host", self)
        self.extensions.load()
        self._ext_command_names: set[str] = set()
        self._sync_extension_commands()

        # 会话管理（保存在 workspace 下，不污染用户主目录）
        self._session_manager = SessionManager(
            sessions_root=self.workspace / ".dot" / "sessions",
            workspace=self.workspace,
        )
        self._session = self._session_manager.get_or_create()

        # 工具列表（内置 + 扩展注册）
        self._builtin_tools: list[AgentTool] = []
        self._init_tools()

        # 当前 Harness
        self._harness: AgentHarness | None = None
        self._base_system = ""

        # 子系统
        self._trace = TraceController(self.workspace, lambda: self._session)
        self._mcp = McpConnector()
        self._compactor = ContextCompactor(provider=self.provider, model=self.provider.model)

    def _init_tools(self) -> None:
        """初始化内置工具"""
        try:
            from .tools.file_tools import create_read_tool, create_write_tool, create_edit_tool
            from .tools.bash_tool import create_bash_tool
            from .tools.glob_tool import create_glob_tool
            from .tools.grep_tool import create_grep_tool

            state = {"workspace": self.workspace, "permission": self.permission, "mode": self.mode}
            self._builtin_tools = [
                create_read_tool(state),
                create_write_tool(state),
                create_edit_tool(state),
                create_bash_tool(state),
                create_glob_tool(state),
                create_grep_tool(state),
            ]
        except ImportError as exc:
            logger.warning("[host] Failed to load tools: %s", exc)

    @property
    def _tools(self) -> list[AgentTool]:
        """内置工具 + 扩展注册工具 + MCP 远程工具"""
        return self._builtin_tools + self.extensions.tools + list(self._mcp.tools.values())

    def set_mode(self, mode: AgentMode) -> None:
        """切换模式并同步到当前 harness（权限检查读取 harness 配置里的模式快照）"""
        self.mode = mode
        if self._harness is not None:
            self._harness._config.agent_mode = mode.value

    def create_harness(
            self,
            *,
            model: str | None = None,
            system: str = "",
            max_turns: int | None = None,
    ) -> AgentHarness:
        """创建 AgentHarness（若当前会话有历史消息则一并回灌）"""
        self._base_system = system
        # 初始化 AgentHarness 配置： 主要是 模式，模型配置，插件机制（提示词，工具前/后调用扩展， 扩展工具注册）
        config = AgentHarnessConfig(
            provider=self.provider,
            model=model or self.provider.model,
            system=self._compose_system(system),
            tools=self._tools,
            max_turns=max_turns,
            permission=self.permission,
            compaction=AutoCompactor(self._compactor) if self.auto_compact else None,
            agent_mode=self.mode.value,
        )
        before_hook, after_hook = self.extensions.make_tool_hooks()
        config.before_tool_call = before_hook
        config.after_tool_call = after_hook
        self._harness = AgentHarness(config, messages=self._session.messages)
        # AgentEvent 流 -> 扩展生命周期 hook（agent_start / turn_end / ...）
        self.extensions.attach_to_harness(self._harness)
        # 订阅和初始化 TraceCollector
        self._trace.attach(self._harness)
        return self._harness

    # ============================================================
    # 链路追踪（委托 TraceController）
    # ============================================================

    def set_trace_enabled(self, enabled: bool) -> None:
        """运行时开关链路追踪（重挂 collector）"""
        self._trace.set_enabled(enabled, harness=self._harness)

    def trace_info(self) -> dict:
        """追踪状态与落盘目录"""
        return self._trace.info()

    def flush_trace(self) -> None:
        """进程退出前兜底落盘未结束的 span"""
        self._trace.flush()

    def _compose_system(self, base: str) -> str:
        """基础 system prompt + 扩展注册的 prompt sections"""
        full = base
        for section in self.extensions.prompt_sections:
            full += f"\n\n{section}"
        return full

    def refresh_extensions(self) -> int:
        """热重载扩展并同步到当前 harness 与命令注册表，返回成功加载数"""
        loaded = self.extensions.reload()
        if self._harness is not None:
            self._harness.set_tools(self._tools)
            self._harness._config.system = self._compose_system(self._base_system)
            before_hook, after_hook = self.extensions.make_tool_hooks()
            self._harness._config.before_tool_call = before_hook
            self._harness._config.after_tool_call = after_hook
        self._sync_extension_commands()
        return loaded

    def _sync_extension_commands(self) -> None:
        """把扩展注册的命令并入全局 slash 注册表（reload 时先注销旧命令）"""
        from .commands import SlashCommand, SlashResult, get_command_registry

        registry = get_command_registry()
        for name in self._ext_command_names:
            registry.unregister(name)
        self._ext_command_names.clear()

        for name, handler in self.extensions.commands.items():
            def _run(args: str, _handler=handler) -> SlashResult:
                result = _handler(args)
                if isinstance(result, SlashResult):
                    return result
                if isinstance(result, str):
                    return SlashResult(kind="message", text=result)
                return SlashResult(kind="none")

            registry.register(SlashCommand(
                name=name, usage=f"/{name}",
                description="extension command", handler=_run,
            ))
            self._ext_command_names.add(name)

    async def run_workflow(
            self,
            task: str,
            *,
            max_replan: int = 3,
            max_turns: int | None = 30,
            ui_mode: Literal["none", "console", "tui"] = "console",
            human_intervene: "HumanInterventionHandler | None" = None,
            harness: AgentHarness | None = None,
    ) -> AsyncIterator[AgentEvent | WorkflowEvent]:
        """运行完整 coding workflow（plan → code → validate）

        Yields:
            引擎事件（WorkflowEvent）与 Agent 事件
        """
        from .workflow import create_context, run_workflow

        ctx = create_context(task, max_replan=max_replan)
        async for event in run_workflow(
                ctx,
                self,
                max_turns=max_turns,
                ui_mode=ui_mode,
                human_intervene=human_intervene,
                harness=harness,
        ):
            yield event

    # ============================================================
    # Session 管理（委托 SessionManager / Session）
    # ============================================================

    @property
    def session_id(self) -> str:
        """当前会话 ID"""
        return self._session.session_id

    @property
    def session(self) -> Session:
        """当前会话对象"""
        return self._session

    def save_session(self) -> None:
        """兼容入口：等价于 end_turn（增量落盘 + git 快照）"""
        self.end_turn()

    def end_turn(self) -> int:
        """一轮对话结束：把 harness 消息同步进 session，增量落盘 + git 快照

        返回本轮 turn_id。
        """
        if self._harness is not None:
            self._session.messages = list(self._harness.messages)
        new_messages = self._session.messages[self._session._persisted_count:]
        turn_id = self._session_manager.commit_turn(new_messages)
        return turn_id

    def list_turns(self) -> list[dict]:
        """列出当前会话的所有轮次（/rewind 列表用）"""
        return self._session.list_turns()

    def rewind_to_turn(self, turn_id: int) -> dict:
        """回滚到指定轮次：截断消息历史 + 恢复 workspace 文件（git reset）"""
        commit = self._session_manager.rewind(turn_id)
        if self._harness is not None:
            self._harness.replace_messages(self._session.messages)
        return {
            "turn_id": turn_id, "commit": commit,
            "messages": len(self._session.messages),
        }

    def resume_session(self, session_id: str) -> bool:
        """切换到指定历史会话并把消息回灌到当前 harness

        返回是否成功恢复（会话不存在返回 False，保持原会话不变）。
        """
        if not self._session_manager.session_storage.exists(session_id):
            logger.warning("[host] Session not found: %s", session_id)
            return False
        try:
            session = self._session_manager.switch_to(session_id)
        except Exception:
            logger.exception("[host] Resume failed: %s", session_id)
            return False
        self._session = session
        if self._harness is not None:
            self._harness.replace_messages(session.messages)
        return True

    def list_sessions(self) -> list[dict]:
        """列出所有历史会话"""
        return self._session_manager.list_sessions()

    # ============================================================
    # MCP 远程工具（委托 McpConnector）
    # ============================================================

    async def connect_mcp(self) -> str:
        """连接 .dot/mcp.json 里配置的 MCP 服务器，把远程工具绑定进工具列表

        在事件循环内调用（console / TUI 启动时）。返回报告文本。
        """
        report = await self._mcp.connect(self.workspace)
        if self._mcp.tools and self._harness is not None:
            self._harness.set_tools(self._tools)
        return report

    def list_mcp_servers(self) -> list[dict]:
        """已配置 MCP 服务器的连接状态"""
        return self._mcp.list_servers()

    # ============================================================
    # 上下文压缩（/compact，委托 ContextCompactor）
    # ============================================================

    def compact_context(self) -> str:
        """应用压缩：L1/L2 同步执行；L3 级别时调度异步 LLM 摘要后替换消息

        返回给人看的报告文本。压缩结果同步写回 harness 与 session。
        """
        if self._harness is None:
            return "no harness"
        self._session.messages = list(self._harness.messages)

        outcome = self._compactor.compact(self._session.messages)
        if outcome.scheduled_l3:
            self._compactor.schedule_l3(
                outcome.messages,
                on_done=self._apply_l3_result,
            )

        self._harness.replace_messages(outcome.messages)
        self._session.messages = list(outcome.messages)
        self._session._persisted_count = min(self._session._persisted_count, len(outcome.messages))
        self.save_session()
        return outcome.report

    def _apply_l3_result(self, compacted: list) -> None:
        """L3 异步摘要完成后的写回：替换 harness 与 session 消息"""
        if self._harness is not None:
            self._harness.replace_messages(compacted)
        self._session.messages = list(compacted)
        self.save_session()
