"""
渐进披露元工具 + 特殊分发（对齐 doc/fix.md）

元工具（注册进 LLM tools 数组）：
- mcp_search(tool_name=""):
    无参数 → 返回全部 MCP 工具目录（name + 描述）
    带 tool_name → 返回该工具完整定义（description + input_schema）并加载
- skill_search(skill_name): 返回 Skill 完整指令内容

特殊分发（dispatch_special_tool，coding/valid 节点每轮调用）：
- mcp_ 开头（非 mcp_search）：
    已加载 → 交回正常执行链（execute_tool_by_name）
    未加载 → 返回工具定义给模型（不报错），下一轮带 schema 参数再调即执行
- skill_ 开头（非 skill_search）→ 返回 Skill 完整内容
- 其余 → None（系统工具，正常执行）

agent 工作模式（fix.md）：
- plan: 只读工具集（FileRead/Glob/Grep/只读 Bash）+ 元工具
- edit: 全工具 + 每次 Bash 审批
- auto: 全工具，权限最大
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from ..core.log import get_logger
from ..core.runtime import RuntimeState

logger = get_logger(__name__)


# ============================================================
# 元工具构建
# ============================================================

def build_mcp_search_tool(session: Any) -> StructuredTool | None:
    """构建 mcp_search 元工具（session.mcp_host 为 None 时返回 None）"""
    host = getattr(session, "mcp_host", None)
    if host is None:
        return None

    def mcp_search(tool_name: str = "") -> dict[str, Any]:
        """搜索 MCP 工具：无参数返回目录；带 tool_name 返回完整定义并加载"""
        runtime = getattr(session, "runtime", None)
        try:
            if not tool_name:
                tools = []
                for name in host.get_all_tool_names():
                    schema = host._schema_cache.get(name, {})
                    tools.append({
                        "name": name,
                        "description": schema.get("description", ""),
                        "loaded": host.is_tool_loaded(name),
                    })
                return {"ok": True, "tools": tools}
            # 指定工具名 → 返回完整定义并注册可调用包装
            try:
                schema = host.load_tool_schema(tool_name)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if runtime is not None and tool_name not in runtime.loaded_mcp_tools:
                runtime.loaded_mcp_tools[tool_name] = _make_mcp_callable(session, tool_name)
            return {
                "ok": True,
                "tool_name": tool_name,
                "description": schema.get("description", ""),
                "input_schema": schema.get("input_schema", {}),
                "message": "请按 input_schema 构造参数后调用该工具。",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        name="mcp_search",
        func=mcp_search,
        description=(
            "搜索 MCP 外部工具。不带参数返回全部工具目录（name + 描述）；"
            "带 tool_name 返回该工具完整定义（description + input_schema）并加载为可调用。"
            "调用任何 mcp_ 开头的工具前应先用它获取定义。"
        ),
    )


def build_skill_search_tool(session: Any) -> StructuredTool | None:
    """构建 skill_search 元工具（session.skill_host 为 None 时返回 None）"""
    host = getattr(session, "skill_host", None)
    if host is None:
        return None

    def skill_search(skill_name: str = "") -> dict[str, Any]:
        """获取指定 Skill 的完整指令内容"""
        try:
            if not skill_name:
                return {
                    "ok": True,
                    "skills": host.get_all_skill_names(),
                    "hint": "带 skill_name 参数获取完整内容",
                }
            return host.invoke_skill(skill_name)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        name="skill_search",
        func=skill_search,
        description=(
            "获取指定 Skill 的完整指令内容（skill_name 形如 skill_xxx）。"
            "拿到内容后严格按 Skill 内的规则执行任务。"
        ),
    )


# ============================================================
# 特殊分发（mcp_ / skill_ 前缀）
# ============================================================

def dispatch_special_tool(session: Any, call: dict[str, Any]) -> ToolMessage | None:
    """渐进披露特殊分发（对齐 fix.md 的执行规则）

    Returns:
        ToolMessage —— 已特殊处理（返回定义 / skill 内容 / 错误）
        None        —— 非特殊工具，交回正常执行链
    """
    name = call.get("name", "")
    tool_call_id = call.get("id") or f"{name}-call"

    def _tm(payload: dict[str, Any]) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            name=name,
            tool_call_id=tool_call_id,
        )

    # mcp_ 前缀（元工具本身除外）
    if name.startswith("mcp_") and name != "mcp_search":
        host = getattr(session, "mcp_host", None)
        runtime = getattr(session, "runtime", None)
        if host is None:
            return None
        loaded = (runtime is not None and name in runtime.loaded_mcp_tools) or host.is_tool_loaded(name)
        if loaded:
            # 已加载：若 runtime 里还没有可调用包装则补注册，然后交回正常执行
            if runtime is not None and name not in runtime.loaded_mcp_tools:
                runtime.loaded_mcp_tools[name] = _make_mcp_callable(session, name)
            return None
        # 未加载：返回工具定义（不报错），模型看到定义后按 schema 重新调用
        schema = host._schema_cache.get(name)
        if schema is None:
            available = host.get_all_tool_names()[:30]
            return _tm({
                "ok": False,
                "error": f"unknown mcp tool: {name}",
                "available": available,
                "hint": "先调用 mcp_search 查看工具目录",
            })
        host._loaded_tools.add(name)
        if runtime is not None:
            runtime.loaded_mcp_tools[name] = _make_mcp_callable(session, name)
        return _tm({
            "ok": True,
            "tool_name": name,
            "description": schema.get("description", ""),
            "input_schema": schema.get("input_schema", {}),
            "message": "工具已加载，请按 input_schema 构造参数后重新调用以执行。",
        })

    # skill_ 前缀（元工具本身除外）：返回 skill 完整内容
    if name.startswith("skill_") and name != "skill_search":
        host = getattr(session, "skill_host", None)
        if host is None:
            return None
        result = host.invoke_skill(name)
        return _tm(result)

    return None


def _make_mcp_callable(session: Any, tool_name: str) -> StructuredTool:
    """为已加载的 mcp_ 工具创建可调用包装（转发 MCPToolExecutor）"""
    from ..mcp.host import MCPToolExecutor

    host = session.mcp_host
    executor = MCPToolExecutor(host)

    def _invoke(**kwargs: Any) -> dict[str, Any]:
        try:
            result = executor.execute({"name": tool_name, "args": kwargs})
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        except Exception as exc:
            return {"ok": False, "is_error": True, "error": f"{type(exc).__name__}: {exc}"}

    schema = host._schema_cache.get(tool_name, {})
    desc = schema.get("description", "") or f"MCP tool {tool_name}"
    return StructuredTool.from_function(name=tool_name, func=_invoke, description=desc)


# ============================================================
# 会话工具集构建（按 agent_mode 选择）
# ============================================================

def build_tools_for_session(session: Any) -> list[StructuredTool]:
    """为 session 构建完整工具列表：基础工具（按 agent_mode）+ 元工具

    - plan: 只读工具（FileRead/Glob/Grep + 只读 Bash）+ mcp_search/skill_search
    - edit / auto: 全工具 + 元工具（edit 的 bash 审批由 bash_tool 按 agent_mode 处理）
    """
    from . import build_read_only_tools, build_tools

    runtime: RuntimeState | None = getattr(session, "runtime", None)
    if runtime is None:
        runtime = RuntimeState(
            workspace=session.workspace,
            hook_runner=getattr(session, "hook_runner", None),
            session_id=getattr(session, "session_id", ""),
        )
        session.runtime = runtime

    agent_mode = getattr(runtime, "agent_mode", "auto") or "auto"
    if agent_mode == "plan":
        tools: list[StructuredTool] = list(build_read_only_tools(runtime))
    else:
        tools = list(build_tools(runtime))

    mcp_tool = build_mcp_search_tool(session)
    if mcp_tool is not None:
        tools.append(mcp_tool)
    skill_tool = build_skill_search_tool(session)
    if skill_tool is not None:
        tools.append(skill_tool)

    return tools
