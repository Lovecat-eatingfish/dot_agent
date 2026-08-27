"""
AgentContext — 进程级组件容器

生命周期 = AgentHost（进程启动时创建一次，所有 Session 共享引用）。
与 Session 分离的原因：组件是服务，不是状态；生命周期不同；图节点
需要访问这些组件但不该依赖 Session 的具体字段。

持有：
  - mcp_host:       MCP 外部工具连接
  - skill_host:     Skill 发现与加载
  - hook_runner:    生命周期 Hook 执行器
  - permission:     三级权限管控
  - tracer:         链路追踪
  - compiled_graph: LangGraph 编译产物

使用方式（graph 节点内）：
    ctx = state["context"]
    tools = build_tools_for_session(session, ctx)
    catalog = ctx.mcp_host.get_catalog_text(...)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.hooks import HookRunner
    from ..core.permission import PermissionManager
    from ..core.tool_result_budget import ToolResultBudget
    from ..mcp.host import MCPHost
    from ..mcp.manager import MCPManager
    from ..skills.host import SkillHost
    from ..skills.manager import SkillsManager
    from ..trace import Tracer
    from langgraph.graph.state import CompiledStateGraph


@dataclass
class AgentContext:
    """进程级组件容器（AgentHost 持有，图节点通过 state["context"] 访问）"""

    # MCP 外部工具
    mcp_host: MCPHost | None = None
    mcp_manager: MCPManager | None = None

    # Skill 发现
    skill_host: SkillHost | None = None
    skills_manager: SkillsManager | None = None

    # 生命周期 Hook
    hook_runner: HookRunner | None = None

    # 权限管控
    permission_manager: PermissionManager | None = None

    # 链路追踪
    tracer: Tracer | None = None

    # LangGraph 编译产物
    compiled_graph: CompiledStateGraph | None = None

    # --- 渐进披露运行时状态 ---
    # MCP 工具：已按需加载的工具名 → StructuredTool
    loaded_mcp_tools: dict = field(default_factory=dict)
    # Skill：已加载的 skill 名集合 + 累计注入正文
    loaded_skills: set = field(default_factory=set)
    active_skill_content: str = ""

    # --- 工具输出预算 ---
    result_budget: ToolResultBudget | None = None
