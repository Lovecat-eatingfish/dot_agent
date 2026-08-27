"""
ContextBuilder — 进程级组件构建器

从 AgentHost._build_context 抽取，职责：
  - 构建 Tracer
  - 构建 PermissionManager
  - 构建 MCPHost
  - 构建 SkillHost
  - 构建 HookRunner
  - 构建 CompiledGraph
  - 构建 ToolResultBudget
  - 组装 AgentContext

使用方式：
    builder = ContextBuilder(workspace=Path.cwd())
    ctx = builder.build()
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.log import get_logger
from ..session.agent_context import AgentContext

logger = get_logger(__name__)


class ContextBuilder:
    """进程级组件构建器"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.warnings: list[str] = []

    def build(self) -> AgentContext:
        """构建完整的 AgentContext"""
        ctx = AgentContext()

        # Tracer
        ctx.tracer = self.build_tracer()

        # Permission
        ctx.permission_manager = self.build_permission_manager()

        # MCP
        ctx.mcp_manager, ctx.mcp_host = self.build_mcp_host()

        # Skills
        ctx.skills_manager, ctx.skill_host = self.build_skill_host()

        # Hooks
        ctx.hook_runner = self.build_hook_runner()

        # Compiled graph
        ctx.compiled_graph = self.build_compiled_graph()

        # Tool result budget
        ctx.result_budget = self.build_result_budget()

        if self.warnings:
            logger.warning("[ContextBuilder] %d warnings: %s", len(self.warnings), "; ".join(self.warnings))
        logger.info("[ContextBuilder] context ready, workspace=%s", self.workspace)
        return ctx

    def build_tracer(self) -> Any:
        """构建 Tracer"""
        from ..trace import get_tracer
        return get_tracer()

    def build_permission_manager(self) -> Any:
        """构建 PermissionManager"""
        from ..core.permission import get_permission_manager
        pm = get_permission_manager()
        pm.load_project(self.workspace)
        return pm

    def build_mcp_host(self) -> tuple[Any, Any]:
        """构建 MCPManager 和 MCPHost"""
        from ..mcp.host import MCPHost
        from ..mcp.manager import MCPManager

        manager = MCPManager(workspace=self.workspace)
        try:
            manager.load_config_and_connect()
        except Exception as exc:
            msg = f"MCP 连接失败: {exc}"
            logger.warning("[ContextBuilder] %s", msg)
            self.warnings.append(msg)

        host = MCPHost(manager)
        try:
            host.discover_tools()
        except Exception as exc:
            msg = f"MCP 工具发现失败: {exc}"
            logger.warning("[ContextBuilder] %s", msg)
            self.warnings.append(msg)

        return manager, host

    def build_skill_host(self) -> tuple[Any, Any]:
        """构建 SkillsManager 和 SkillHost"""
        from ..skills.host import SkillHost
        from ..skills.manager import SkillsManager

        manager = SkillsManager(workspace=self.workspace)
        host = SkillHost(manager)
        try:
            host.discover_skills()
        except Exception as exc:
            msg = f"Skill 发现失败: {exc}"
            logger.warning("[ContextBuilder] %s", msg)
            self.warnings.append(msg)

        return manager, host

    def build_hook_runner(self) -> Any:
        """构建 HookRunner"""
        from ..core.hooks import HookRunner
        from ..core.hook_loader import load_hooks_into_runner

        runner = HookRunner()
        try:
            load_hooks_into_runner(runner, self.workspace)
        except Exception as exc:
            msg = f"Hook 加载失败: {exc}"
            logger.warning("[ContextBuilder] %s", msg)
            self.warnings.append(msg)

        return runner

    def build_compiled_graph(self) -> Any:
        """构建 LangGraph 编译产物"""
        from ..graph.coding_graph import compile_graph
        return compile_graph()

    def build_result_budget(self) -> Any:
        """构建 ToolResultBudget"""
        from ..core.tool_result_budget import ToolResultBudget
        return ToolResultBudget()
