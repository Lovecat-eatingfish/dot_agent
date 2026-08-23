"""
SharedServices — per-workspace 共享设施容器

生命周期 = AgentHost：创建一次，所有 Session 复用同一份，避免每会话各自起
MCP bridge 线程 / 重连 server / 重新扫描 skills。

持有：
  - MCPManager + MCPHost（单条 bridge 线程 + 单套 server 连接 + 发现数据）
  - SkillsManager + SkillHost（单套 skill 发现）
  - HookRunner（无状态 handler，会话信息靠 payload.session_id 传入）
  - compiled_graph（无 checkpointer，节点状态全从 state["session"] 取，可重入）
  - Tracer / PermissionManager（本就是模块单例，此处引用便于 init 编排）

与 runtime_registry 的分工：
  - SharedServices：共享、init 后不可变的对象
  - runtime_registry：每个 session 私有的易变对象（RuntimeState / persistence）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.hook_loader import load_hooks_into_runner
from ..core.hooks import HookRunner
from ..core.log import get_logger
from ..core.permission import get_permission_manager
from ..trace import get_tracer

logger = get_logger(__name__)


class SharedServices:
    """per-workspace 共享设施容器（AgentHost 持有，所有 Session 引用）"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

        # 已是模块单例——引用便于 init 编排（init_tracer / load_project 已先完成）
        self.tracer = get_tracer()
        self.permission_manager = get_permission_manager()

        # 共享 MCP（单条 bridge 线程 + 单套连接；无 .dot/mcp.json 时 0 连接，不报错）
        from ..mcp.host import MCPHost
        from ..mcp.manager import MCPManager

        self.mcp_manager = MCPManager(workspace=workspace)
        try:
            self.mcp_manager.load_config_and_connect()
        except Exception as exc:
            logger.warning("SharedServices: mcp connect failed: %s", exc)
        self.mcp_host = MCPHost(self.mcp_manager)
        try:
            self.mcp_host.discover_tools()
        except Exception as exc:
            logger.warning("SharedServices: mcp discover failed: %s", exc)

        # 共享 Skills 发现
        from ..skills.host import SkillHost
        from ..skills.manager import SkillsManager

        self.skills_manager = SkillsManager(workspace=workspace)
        self.skill_host = SkillHost(self.skills_manager)
        try:
            self.skill_host.discover_skills()
        except Exception as exc:
            logger.debug("SharedServices: skill discover skipped: %s", exc)

        # 共享 HookRunner（无状态 handler）
        self.hook_runner = HookRunner()
        try:
            load_hooks_into_runner(self.hook_runner, workspace)
        except Exception as exc:
            logger.debug("SharedServices: hook load skipped: %s", exc)

        # 共享 compiled_graph（无 checkpointer，可重入）
        from ..graph.coding_graph import compile_graph

        self.compiled_graph = compile_graph()

        logger.info("[SharedServices] ready, workspace=%s", workspace)

    def close(self) -> None:
        """关闭共享资源（AgentHost 销毁时调用；不随单个 session 关闭）"""
        try:
            self.mcp_host.close()
        except Exception as exc:
            logger.debug("SharedServices close: mcp: %s", exc)
