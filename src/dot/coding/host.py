"""
dot.coding.host — CodingHost（新架构组装层）

组装 dot.ai + dot.agent + dot.coding 三层，
提供统一的执行入口。集成 SessionManager 实现消息持久化。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from dot.agent.events import AgentEvent
from dot.agent.harness import AgentHarness, AgentHarnessConfig
from dot.agent.tools import AgentTool
from dot.ai.catalog import ProviderCatalog
from dot.ai.providers.openai import OpenAIProvider

from .extensions.runtime import ExtensionRuntime
from .modes import AgentMode
from .permission import PermissionManager, get_permission_manager
from .session import Session
from .session.manager import SessionManager
from .state import WorkflowContext, WorkflowPhase

logger = logging.getLogger(__name__)


class CodingHost:
    """Coding Agent 主机组装

    一个 CodingHost = 一个工作空间 = Provider + Agent + Tools + Permission + Extensions + Session
    """

    def __init__(
            self,
            workspace: Path | None = None,
            *,
            mode: AgentMode = AgentMode.AUTO,
            extra_extension_dirs: list[Path] | None = None,
    ) -> None:
        from .cli.console_app import approval_handler
        self.workspace = workspace or Path.cwd()
        self.mode = mode

        # 加载 Provider 配置
        # todo： 暂时实现env的配置，配置大模型参数，后面改为文件配置
        self.catalog = ProviderCatalog.load()
        self.provider = OpenAIProvider()

        # 权限管理
        self.permission = get_permission_manager()
        self.permission.load_project(self.workspace)
        # 设置审批处理函数
        self.permission.set_approval_handler(approval_handler)

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
        self._trace_unsub: Callable[[], None] | None = None
        self._trace_collector = None

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
        """内置工具 + 扩展注册工具"""
        return self._builtin_tools + self.extensions.tools

    def set_mode(self, mode: AgentMode) -> None:
        self.mode = mode

    def create_harness(
            self,
            *,
            model: str | None = None,
            system: str = "",
            max_turns: int | None = None,
    ) -> AgentHarness:
        """创建 AgentHarness（若当前会话有历史消息则一并回灌）"""
        self._base_system = system
        config = AgentHarnessConfig(
            provider=self.provider,
            model=model or self.provider.model,
            system=self._compose_system(system),
            tools=self._tools,
            max_turns=max_turns,
        )
        before_hook, after_hook = self.extensions.make_tool_hooks()
        config.before_tool_call = before_hook
        config.after_tool_call = after_hook
        self._harness = AgentHarness(config, messages=self._session.messages)
        self._attach_trace()
        return self._harness

    # ============================================================
    # 链路追踪
    # ============================================================

    def _attach_trace(self) -> None:
        """为当前 harness 订阅 TraceCollector（DOT_TRACE_ENABLED=0 时为 Noop）"""
        from .trace import make_trace_collector

        self._detach_trace()
        self._trace_collector = make_trace_collector(self.workspace, self._session.session_id)
        if self._harness is not None:
            self._trace_unsub = self._harness.subscribe(self._trace_collector.on_event)

    def _detach_trace(self) -> None:
        if self._trace_unsub is not None:
            self._trace_unsub()
            self._trace_unsub = None

    def set_trace_enabled(self, enabled: bool) -> None:
        """运行时开关链路追踪（重挂 collector）"""
        import os

        os.environ["DOT_TRACE_ENABLED"] = "1" if enabled else "0"
        self._attach_trace()

    def trace_info(self) -> dict:
        """追踪状态与落盘目录"""
        from .trace import trace_enabled as _enabled

        return {
            "enabled": _enabled(),
            "session_id": self._session.session_id,
            "output_dir": self.workspace / ".dot" / "traces",
        }

    def flush_trace(self) -> None:
        """进程退出前兜底落盘未结束的 span"""
        if self._trace_collector is not None:
            self._trace_collector.flush()

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
            model: str | None = None,
    ) -> AsyncIterator[AgentEvent | WorkflowPhase]:
        from .workflow import run_workflow
        """运行完整 workflow"""
        context = WorkflowContext(task=task)
        async for event in run_workflow(context, self):
            yield event

    # ============================================================
    # Session 管理
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
        """将当前 harness 的消息同步到 session 并持久化"""
        if self._harness is not None:
            self._session.messages = list(self._harness.messages)
        self._session_manager.session_storage.save(self._session)
        logger.debug("[host] Session saved: %s", self._session.session_id)

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
