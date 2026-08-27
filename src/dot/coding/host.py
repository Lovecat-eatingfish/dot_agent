"""
dot.coding.host — CodingHost（新架构组装层）

组装 dot.ai + dot.agent + dot.coding 三层，
提供统一的执行入口。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from dot.agent.events import AgentEvent
from dot.agent.harness import AgentHarness, AgentHarnessConfig
from dot.agent.tools import AgentTool
from dot.ai.catalog import ProviderCatalog
from dot.ai.providers.openai import OpenAIProvider

from .modes import AgentMode
from .permission import PermissionManager, get_permission_manager
from .state import WorkflowContext, WorkflowPhase

logger = logging.getLogger(__name__)


class CodingHost:
    """Coding Agent 主机组装

    一个 CodingHost = 一个工作空间 = Provider + Agent + Tools + Permission
    """

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        model: str | None = None,
        mode: AgentMode = AgentMode.AUTO,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.mode = mode

        # 加载 Provider 配置
        self.catalog = ProviderCatalog.load()
        self.provider = OpenAIProvider()

        # 权限管理
        self.permission = get_permission_manager()
        self.permission.load_project(self.workspace)

        # 工具列表（延迟初始化）
        self._tools: list[AgentTool] = []
        self._init_tools()

        # 当前 Harness
        self._harness: AgentHarness | None = None

    def _init_tools(self) -> None:
        """初始化内置工具"""
        try:
            from .tools.file_tools import create_read_tool, create_write_tool, create_edit_tool
            from .tools.bash_tool import create_bash_tool
            from .tools.glob_tool import create_glob_tool
            from .tools.grep_tool import create_grep_tool

            state = {"workspace": self.workspace, "permission": self.permission, "mode": self.mode}
            self._tools = [
                create_read_tool(state),
                create_write_tool(state),
                create_edit_tool(state),
                create_bash_tool(state),
                create_glob_tool(state),
                create_grep_tool(state),
            ]
        except ImportError as exc:
            logger.warning("[host] Failed to load tools: %s", exc)

    def set_mode(self, mode: AgentMode) -> None:
        self.mode = mode

    def create_harness(
        self,
        *,
        model: str | None = None,
        system: str = "",
        max_turns: int | None = None,
    ) -> AgentHarness:
        """创建 AgentHarness"""
        config = AgentHarnessConfig(
            provider=self.provider,
            model=model or "gpt-4o",
            system=system,
            tools=self._tools,
            max_turns=max_turns,
        )
        self._harness = AgentHarness(config)
        return self._harness

    async def run_workflow(
        self,
        task: str,
        *,
        model: str | None = None,
    ) -> AsyncIterator[AgentEvent | WorkflowPhase]:
        """运行完整 workflow"""
        context = WorkflowContext(task=task)
        async for event in run_workflow(context):
            yield event
