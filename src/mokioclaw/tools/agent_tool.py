"""
Agent 工具 — 派生子 Agent

对齐 Claude Code：
- 子 Agent 拥有独立 messages / SnapshotTracker / queryLoop
- 工具执行走 gate + hooks + L1 budget（与主 Agent 同一契约）
- 默认禁止再嵌套 Agent（防无限 fork）
- model=inherit：复用父静态 system 前缀
- run_in_background=true：立刻返回 taskId；Cancel 协作式 abort
"""
from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import execute_tool_by_name, last_ai_content
from mokioclaw.prompts.builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY, PromptBuilder
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.reliability.background_tasks import get_background_registry, run_in_thread
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools.registry import TOOL_CONCURRENCY_META, build_tools

logger = get_logger(__name__)

# task_id → 正在运行的子 Runtime（用于 Cancel 置 _abort）
_ACTIVE_CHILDREN: dict[str, RuntimeState] = {}

# 默认子 Agent 工具集；AgentTool 由深度限制单独控制
_DEFAULT_SUBAGENT_TOOLS = frozenset({
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GlobTool",
    "GrepTool",
    "BashTool",
    "WebSearchTool",
    "SkillTool",
    "MemoryIndexTool",
    "MemoryReadTool",
    "LoadMcpTool",
    # 嵌套子代理（默认允许，深度上限内）
    "AgentTool",
})


def max_subagent_depth() -> int:
    """最大嵌套深度（对齐 Claude Code：子代理最多再向下 3 层）"""
    try:
        value = int(os.getenv("MOKIO_MAX_SUBAGENT_DEPTH", "3"))
    except ValueError:
        return 3
    return max(1, min(value, 5))


def fork_subagent(
    parent: RuntimeState,
    *,
    task: str,
    model: str = "inherit",
    allowed_tools: list[str] | None = None,
    max_loops: int = 8,
    disable_nested_agent: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """同步派生子 Agent，返回精简报告"""
    child = _spawn_child_runtime(parent)
    if task_id:
        _ACTIVE_CHILDREN[task_id] = child
    try:
        tools = _filter_child_tools(child, allowed_tools, disable_nested_agent=disable_nested_agent)
        system_prompt = _build_child_system(parent, child, task=task, model=model)
        bound = create_model().bind_tools(tools)
        messages: list[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]

        for _ in range(max_loops):
            if getattr(child, "_abort", False):
                return {"ok": False, "error": "subagent cancelled", "summary": ""}
            # MCP LoadMcpTool 后需要 rebind
            tools = _filter_child_tools(child, allowed_tools, disable_nested_agent=disable_nested_agent)
            bound = create_model().bind_tools(tools)

            response = bound.invoke(messages)
            messages.append(response)
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                break
            # LoadMcpTool 优先；加载后立刻刷新 tools，同批后续调用可用
            calls = _order_calls_load_mcp_first(calls)
            for call in calls:
                if getattr(child, "_abort", False):
                    return {"ok": False, "error": "subagent cancelled", "summary": ""}
                tool_message = execute_tool_by_name(
                    tools,
                    call,
                    hook_runner=child.hook_runner,
                    budget=child.result_budget,
                    workspace=child.workspace,
                    runtime=child,
                )
                messages.append(tool_message)
                if call.get("name") == "LoadMcpTool":
                    tools = _filter_child_tools(
                        child, allowed_tools, disable_nested_agent=disable_nested_agent
                    )

        summary = last_ai_content(messages) or "Subagent finished without a textual summary."
        # Handoff 分类器：交回父级前审查子 Agent 行为摘要（对齐 Claude Code classifyHandoff）
        try:
            from mokioclaw.security.classifier import classify_handoff

            decision, reason = classify_handoff(task, summary)
        except Exception:  # noqa: BLE001
            decision, reason = None, "classifier unavailable"
        result: dict[str, Any] = {"ok": True, "summary": summary, "model": model}
        if decision is not None and decision.value != "allow":
            # DENY / ASK：把审查结论作为 warning 附在结果里，供父级感知
            result["handoff_warning"] = f"[{decision.value}] {reason}"
            if decision.value == "deny":
                result["ok"] = False
                result["error"] = f"handoff blocked by classifier: {reason}"
        return result
    except Exception as exc:
        logger.warning("subagent failed: %s", exc, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "summary": ""}
    finally:
        try:
            from mokioclaw.core.hooks import fire_stop_hook

            fire_stop_hook(
                child.hook_runner,
                workspace=str(child.workspace),
                subagent=True,
            )
        except Exception:
            pass
        if task_id:
            _ACTIVE_CHILDREN.pop(task_id, None)


def abort_subagent(task_id: str) -> bool:
    """协作式取消：置 child._abort，循环内检查后退出"""
    child = _ACTIVE_CHILDREN.get(task_id)
    if child is None:
        return False
    child._abort = True  # type: ignore[attr-defined]
    return True


def _spawn_child_runtime(parent: RuntimeState) -> RuntimeState:
    """独立子 Runtime：独立快照表，共享 workspace / hooks / budget / 审批"""
    child = RuntimeState(
        workspace=parent.workspace,
        approval_mode=parent.approval_mode,
        approval_handler=parent.approval_handler,
        bash_default_timeout_seconds=parent.bash_default_timeout_seconds,
        bash_max_timeout_seconds=parent.bash_max_timeout_seconds,
        bash_max_output_chars=parent.bash_max_output_chars,
        bash_env_file=parent.bash_env_file,
        checkpoint_mode="off",
        trace_mode="off",
        agent_mode=getattr(parent, "agent_mode", "auto"),
        thinking_instruction=getattr(parent, "thinking_instruction", ""),
    )
    child.read_files = {}
    child.file_state_map = {}
    # 继承父级 cd 后的工作目录，避免子代理 Bash 命令丢失 cwd 上下文（#6）
    child.cwd = getattr(parent, "cwd", None)  # type: ignore[attr-defined]
    child.loaded_mcp_tools = dict(getattr(parent, "loaded_mcp_tools", {}) or {})
    child.hook_runner = parent.hook_runner
    child.result_budget = parent.result_budget
    child._abort = False  # type: ignore[attr-defined]
    child._is_subagent = True  # type: ignore[attr-defined]
    child._subagent_depth = int(getattr(parent, "_subagent_depth", 0)) + 1  # type: ignore[attr-defined]
    return child


def _order_calls_load_mcp_first(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        call
        for _, call in sorted(
            enumerate(calls),
            key=lambda item: (0 if item[1].get("name") == "LoadMcpTool" else 1, item[0]),
        )
    ]


def _filter_child_tools(
    child: RuntimeState,
    allowed_tools: list[str] | None,
    *,
    disable_nested_agent: bool,
) -> list[StructuredTool]:
    all_tools = build_tools(child, include_mcp=True, include_skills=True)
    allow = set(allowed_tools) if allowed_tools else set(_DEFAULT_SUBAGENT_TOOLS)
    # 到达深度上限时收回 AgentTool（对齐 Claude Code 深度限制行为）
    at_depth_limit = int(getattr(child, "_subagent_depth", 0)) >= max_subagent_depth()
    if disable_nested_agent or at_depth_limit:
        allow.discard("AgentTool")
        allow.discard("BackgroundTaskStatus")
        allow.discard("BackgroundTaskCancel")
    # MCP 工具放行规则（对齐 Claude Code resolveAgentTools）：
    # - 'mcp__*' 通配符：放行全部 MCP 工具
    # - 具体名 'mcp__server__tool'：只放行自身（由上面 t.name in allow 处理）
    # 原实现用 allow_mcp = any(x.startswith('mcp__')) 把"具体名"也当通配符，
    # 导致指定单个 MCP 工具时放行全部 MCP 工具（#7）。
    allow_all_mcp = "mcp__*" in allow
    filtered = []
    for t in all_tools:
        if t.name in allow:
            filtered.append(t)
        elif t.name.startswith("mcp__") and allow_all_mcp:
            filtered.append(t)
        elif t.name == "LoadMcpTool" and ("LoadMcpTool" in allow or allow_all_mcp):
            filtered.append(t)
    return filtered


def _build_child_system(parent: RuntimeState, child: RuntimeState, *, task: str, model: str) -> str:
    parent_builder = PromptBuilder(workspace=parent.workspace, runtime=parent)
    static, _ = parent_builder.build_parts("code_agent")
    child_builder = PromptBuilder(workspace=child.workspace, runtime=child)
    _, dynamic = child_builder.build_parts("code_agent")
    sub_dynamic = (
        f"{dynamic}\n\n## Subagent Task\n{task}\n"
        if dynamic else f"## Subagent Task\n{task}\n"
    )
    if model == "inherit" and static:
        return f"{static.rstrip()}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n{sub_dynamic}"
    return child_builder.build("code_agent") + f"\n\n## Subagent Task\n{task}\n"


def build_agent_tools(state: RuntimeState) -> list[StructuredTool]:
    """构建 Agent / BackgroundTaskStatus / BackgroundTaskCancel"""

    def _agent(
        task: str,
        model: str = "inherit",
        allowed_tools: list[str] | None = None,
        run_in_background: bool = False,
        max_loops: int = 8,
    ) -> dict[str, Any]:
        depth = int(getattr(state, "_subagent_depth", 0))
        if depth >= max_subagent_depth():
            return {
                "ok": False,
                "is_error": True,
                "error": (
                    f"subagent depth limit reached ({depth}/{max_subagent_depth()}); "
                    "complete the task directly instead of delegating"
                ),
            }

        if not run_in_background:
            return fork_subagent(
                state,
                task=task,
                model=model,
                allowed_tools=allowed_tools,
                max_loops=max_loops,
            )

        registry = get_background_registry()
        bg = registry.create(description=task[:200], model=model)

        def _worker() -> None:
            current = registry.get(bg.task_id)
            if current is not None and current.cancelled:
                return
            result = fork_subagent(
                state,
                task=task,
                model=model,
                allowed_tools=allowed_tools,
                max_loops=max_loops,
                task_id=bg.task_id,
            )
            if result.get("ok"):
                registry.complete(bg.task_id, str(result.get("summary", "")))
            else:
                registry.fail(bg.task_id, str(result.get("error", "unknown")))

        run_in_thread(_worker)
        return {"ok": True, "task_id": bg.task_id, "status": "running"}

    def _status(task_id: str) -> dict[str, Any]:
        return get_background_registry().to_status_dict(task_id)

    def _cancel(task_id: str) -> dict[str, Any]:
        aborted = abort_subagent(task_id)
        task = get_background_registry().cancel(task_id)
        if task is None:
            return {"ok": False, "error": f"unknown task_id: {task_id}"}
        return {
            "ok": True,
            "task_id": task_id,
            "status": task.status,
            "abort_signaled": aborted,
        }

    TOOL_CONCURRENCY_META["AgentTool"] = False
    TOOL_CONCURRENCY_META["BackgroundTaskStatus"] = True
    TOOL_CONCURRENCY_META["BackgroundTaskCancel"] = False

    return [
        StructuredTool.from_function(
            name="AgentTool",
            func=_agent,
            description=(
                "Spawn a subagent with an isolated context to complete a focused task. "
                "Args: task, optional model ('inherit' to reuse parent prompt cache prefix), "
                "optional allowed_tools list, run_in_background, max_loops. "
                "Background mode returns task_id immediately; poll with BackgroundTaskStatus."
            ),
        ),
        StructuredTool.from_function(
            name="BackgroundTaskStatus",
            func=_status,
            description="Poll a background subagent task by task_id. Returns status and final_report when done.",
        ),
        StructuredTool.from_function(
            name="BackgroundTaskCancel",
            func=_cancel,
            description="Cancel a running background subagent task by task_id (cooperative abort).",
        ),
    ]
