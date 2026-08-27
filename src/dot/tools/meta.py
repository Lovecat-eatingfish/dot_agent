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
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from ..core.log import get_logger

if TYPE_CHECKING:
    from ..core.tool_context import ToolContext
    from ..session.agent_context import AgentContext

logger = get_logger(__name__)


# ============================================================
# 元工具构建
# ============================================================

def build_mcp_search_tool(session: ToolContext, ctx: AgentContext | None = None) -> StructuredTool | None:
    """构建 mcp_search 元工具（ctx.mcp_host 为 None 时返回 None）"""
    host = ctx.mcp_host if ctx else None
    if host is None:
        return None

    def mcp_search(tool_name: str = "") -> dict[str, Any]:
        """搜索 MCP 工具：无参数返回目录；带 tool_name 返回完整定义并加载"""
        try:
            if not tool_name:
                loaded = set(ctx.loaded_mcp_tools)
                tools = []
                for name in host.get_all_tool_names():
                    schema = host._schema_cache.get(name, {})
                    tools.append({
                        "name": name,
                        "description": schema.get("description", ""),
                        "loaded": name in loaded,
                    })
                return {"ok": True, "tools": tools}
            # 指定工具名 → 返回完整定义并注册可调用包装
            try:
                schema = host.load_tool_schema(tool_name)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if tool_name not in ctx.loaded_mcp_tools:
                ctx.loaded_mcp_tools[tool_name] = _make_mcp_callable(session, ctx, tool_name)
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
            "调用任何 mcp_ 开头的工具前应先用它获取tool的定义，在调用这个mcp 工具。"
        ),
    )


def build_skill_search_tool(session: ToolContext, ctx: AgentContext | None = None) -> StructuredTool | None:
    """构建 skill_search 元工具（ctx.skill_host 为 None 时返回 None）"""
    host = getattr(ctx, "skill_host", None) if ctx else getattr(session, "skill_host", None)
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
            result = host.invoke_skill(skill_name)
            if result.get("ok"):
                key = result.get("skill_name", "")
                content = result.get("content", "")
                if key and key not in ctx.loaded_skills:
                    ctx.loaded_skills.add(key)
                    ctx.active_skill_content += (
                        f"\n\n--- Skill: {key} ---\n{content}\n--- End of {key} ---\n"
                    )
            return result
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

def dispatch_special_tool(session: ToolContext, ctx: AgentContext, call: dict[str, Any]) -> ToolMessage | None:
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
        host = ctx.mcp_host if ctx else None
        if host is None:
            return None
        if name in ctx.loaded_mcp_tools:
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
        ctx.loaded_mcp_tools[name] = _make_mcp_callable(session, ctx, name)
        return _tm({
            "ok": True,
            "tool_name": name,
            "description": schema.get("description", ""),
            "input_schema": schema.get("input_schema", {}),
            "message": "工具已加载，请按 input_schema 构造参数后重新调用以执行。",
        })

    # skill_ 前缀（元工具本身除外）：返回 skill 完整内容
    if name.startswith("skill_") and name != "skill_search":
        host = ctx.skill_host if ctx else None
        if host is None:
            return None
        result = host.invoke_skill(name)
        if result.get("ok"):
            key = result.get("skill_name", "")
            content = result.get("content", "")
            if key and key not in ctx.loaded_skills:
                ctx.loaded_skills.add(key)
                ctx.active_skill_content += (
                    f"\n\n--- Skill: {key} ---\n{content}\n--- End of {key} ---\n"
                )
        return _tm(result)

    return None


def _make_mcp_callable(session: ToolContext, ctx: AgentContext, tool_name: str) -> StructuredTool:
    """为已加载的 mcp_ 工具创建可调用包装（转发 MCPToolExecutor）

    关键：把 MCP 工具的 input_schema 传给 StructuredTool 的 args_schema，
    这样 model.bind_tools() 时 LLM 能看到完整的参数定义，而不是空 schema。
    """
    from ..mcp.host import MCPToolExecutor

    host = ctx.mcp_host
    executor = MCPToolExecutor(host)
    schema = host._schema_cache.get(tool_name, {})
    desc = schema.get("description", "") or f"MCP tool {tool_name}"
    input_schema = schema.get("input_schema", {})

    def _invoke(**kwargs: Any) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(executor.execute, {"name": tool_name, "args": kwargs})
                result = future.result(timeout=60)
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        except FuturesTimeoutError:
            return {"ok": False, "is_error": True, "error": "MCP tool call timed out after 60s"}
        except Exception as exc:
            return {"ok": False, "is_error": True, "error": f"{type(exc).__name__}: {exc}"}

    # 从 MCP input_schema 构建 pydantic args_schema，让 LLM 看到完整参数定义
    args_schema = _build_args_schema(tool_name, input_schema)
    if args_schema is not None:
        return StructuredTool.from_function(
            name=tool_name, func=_invoke, description=desc, args_schema=args_schema,
        )
    return StructuredTool.from_function(name=tool_name, func=_invoke, description=desc)


def _build_args_schema(tool_name: str, input_schema: dict) -> type | None:
    """从 MCP input_schema 构建 pydantic model（供 StructuredTool.args_schema 使用）

    input_schema 格式示例：
    {"type":"object","properties":{"city":{"type":"string","description":"城市名"}},"required":["city"]}
    """
    from pydantic import Field, create_model

    if not input_schema or not isinstance(input_schema, dict):
        return None
    properties = input_schema.get("properties", {})
    if not properties:
        return None
    required = set(input_schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_def in properties.items():
        prop_type = prop_def.get("type", "string")
        py_type = {"string": str, "integer": int, "number": float, "boolean": bool}.get(prop_type, str)
        desc = prop_def.get("description", "")
        if prop_name in required:
            fields[prop_name] = (py_type, Field(description=desc))
        else:
            default = prop_def.get("default")
            fields[prop_name] = (py_type, Field(default=default, description=desc))
    try:
        return create_model(f"{tool_name}_args", **fields)
    except Exception:
        return None


# ============================================================
# 会话工具集构建（按 agent_mode 选择）
# ============================================================

def build_tools_for_session(session: ToolContext, ctx: AgentContext | None = None) -> list[StructuredTool]:
    """为 session 构建工具列表（按 run_mode 管控工具权限）

    run_mode（CLI 运行模式，存于 session.run_mode，热生效）：
      - agent：全量基础工具 + MCP/Skill 元工具（默认，完整能力）
      - chat：无工具（纯对话，仅 LLM 问答，不读写文件/执行命令/请求 MCP）
      - code：仅安全文件工具（read/write/glob/grep），禁用 Bash、MCP 变更工具

    与 agent_mode（plan/edit/auto，权限细粒度拦截）正交：run_mode 决定
    工具是否暴露给模型，agent_mode 决定暴露后的工具是否需审批/拦截。
    """
    from . import build_tools

    run_mode = getattr(session, "run_mode", "agent")

    # chat 纯对话模式：禁用所有工具
    if run_mode == "chat":
        return []

    tools: list[StructuredTool] = list(build_tools(session))

    # code 代码专注模式：仅保留文件工具，禁用 BashTool
    if run_mode == "code":
        tools = [t for t in tools if t.name != "BashTool"]
        return tools

    # agent 完整模式：全量基础工具 + MCP/Skill 元工具 + 子Agent工具
    mcp_tool = build_mcp_search_tool(session, ctx)
    if mcp_tool is not None:
        tools.append(mcp_tool)
    skill_tool = build_skill_search_tool(session, ctx)
    if skill_tool is not None:
        tools.append(skill_tool)

    # 子Agent工具（同步阻塞 Fork）
    from .subagent import build_subagent_tool
    tools.append(build_subagent_tool(session))

    return tools
