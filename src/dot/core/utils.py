"""
共享工具函数（dot 独立副本）

从旧 core/utils 复制节点和工具直接使用的函数，裁剪掉
tool_gate / context_modifier / microcompact / parallel 依赖：
- 文本处理: truncate / truncate_json / json_safe
- 时间: utc_now
- 文件 I/O: write_json（原子写）
- 消息处理: last_ai_content
- 类型转换: coerce_bool
- 工具执行: execute_tool_by_name（hook 拦截 + budget 落盘 + loaded_mcp_tools 回退）
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage, message_to_dict


# ---------------------------------------------------------------------------
# 工具返回类型（TypedDict）
# ---------------------------------------------------------------------------

class ToolResult(TypedDict, total=False):
    """所有工具返回的基础类型（至少含 ok 字段，失败时含 error）"""
    ok: bool
    error: str


class BashResult(ToolResult, total=False):
    """BashTool 返回类型"""
    timed_out: bool
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    stdout_path: str
    stderr_path: str
    stdout_truncated: bool
    stderr_truncated: bool
    requires_approval: bool
    approval_id: str
    risk_reason: str
    approved: bool
    background: bool
    pid: int


class FileReadResult(ToolResult, total=False):
    """FileReadTool 返回类型"""
    path: str
    total_lines: int
    offset: int
    limit: int
    complete: bool
    content: str


class FileWriteResult(ToolResult, total=False):
    """FileWriteTool 返回类型"""
    type: str  # "create" | "update"
    path: str
    lines: int
    diff: str


class FileEditResult(ToolResult, total=False):
    """FileEditTool 返回类型"""
    path: str
    replacements: int
    diff: str


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

def truncate(text: str, limit: int) -> str:
    """截断文本到指定长度，超出部分用省略号替代"""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def truncate_json(value: Any, limit: int) -> str:
    """将任意值序列化为 JSON 后截断"""
    text = value if isinstance(value, str) else json.dumps(json_safe(value), ensure_ascii=False, default=str)
    return truncate(text, limit)


# ---------------------------------------------------------------------------
# JSON 安全化
# ---------------------------------------------------------------------------

def json_safe(value: Any) -> Any:
    """递归将复杂对象转换为 JSON 可序列化格式

    处理 LangChain 消息、Path、dataclass 等特殊类型。
    """
    if isinstance(value, BaseMessage):
        return message_to_dict(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


# ---------------------------------------------------------------------------
# 时间 / 文件 I/O / JSON 解析
# ---------------------------------------------------------------------------

def utc_now() -> str:
    """当前 UTC 时间的 ISO 格式字符串"""
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON 文件（先写临时文件再替换）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def parse_json_content(content: Any) -> Any:
    """尝试将内容解析为 JSON，失败返回原始内容"""
    try:
        return json.loads(str(content))
    except (json.JSONDecodeError, TypeError):
        return content


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------

def last_ai_content(messages: list[Any]) -> str:
    """从消息列表中提取最后一条非工具消息的文本内容"""
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""


# ---------------------------------------------------------------------------
# 类型转换
# ---------------------------------------------------------------------------

def coerce_bool(value: Any, default: bool = False) -> bool:
    """将任意值转换为布尔值"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        return default
    return default


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------

def execute_tool_by_name(
    tools: list[Any],
    call: dict[str, Any],
    hook_runner: Any | None = None,
    budget: Any | None = None,
    workspace: Any | None = None,
    runtime: Any | None = None,
) -> ToolMessage:
    """按名称查找并执行工具，返回 ToolMessage

    流程：
    1. PreToolUse Hook（可阻断 / 改参数）
    2. 查找工具：tools 列表 → runtime.loaded_mcp_tools 回退（渐进披露刚加载的）
    3. 执行
    4. PostToolUse / PostToolUseFailure Hook
    5. 大输出落盘（budget）

    Args:
        tools: 工具列表（需有 .name 属性）
        call: 工具调用字典，包含 name, args, id
        hook_runner: 可选 HookRunner
        budget: 可选 ToolResultBudget
        workspace: 工作区路径（budget 需要）
        runtime: 可选 RuntimeState（loaded_mcp_tools 回退查找）
    """
    from .hooks import HookEvent, HookPayload, HookRunner
    from .tool_result_budget import ToolResultBudget

    name = call.get("name", "")
    args = call.get("args") or {}
    runtime_obj = runtime or call.get("_runtime")
    tool_call_id = call.get("id") or f"{name}-call"
    mid = (
        runtime_obj.next_message_id()
        if runtime_obj is not None and hasattr(runtime_obj, "next_message_id")
        else None
    )

    def _tm(payload: dict[str, Any]) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            name=name,
            tool_call_id=tool_call_id,
            id=mid,
        )

    # PreToolUse Hook
    if hook_runner and isinstance(hook_runner, HookRunner):
        pre_payload = HookPayload(
            event=HookEvent.PreToolUse,
            tool_name=name,
            tool_args=dict(args),
        )
        pre_result = hook_runner.run(HookEvent.PreToolUse, pre_payload)
        if pre_result.blocked:
            return _tm({"ok": False, "error": pre_result.feedback or "blocked by hook"})
        if pre_result.updated_args is not None:
            args = pre_result.updated_args

    tools_map = {tool.name: tool for tool in tools}
    tool = tools_map.get(name)
    # 渐进披露：mcp_search 刚加载的工具可能不在本轮 tools 列表，回退到 runtime
    if tool is None and runtime_obj is not None:
        loaded = getattr(runtime_obj, "loaded_mcp_tools", None) or {}
        tool = loaded.get(name)
    error: Exception | None = None
    if tool is None:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = tool.invoke(args)
        except Exception as exc:
            error = exc
            result = {"ok": False, "is_error": True, "error": f"{type(exc).__name__}: {exc}"}

    # PostToolUse / PostToolUseFailure Hook
    if hook_runner and isinstance(hook_runner, HookRunner):
        post_event = HookEvent.PostToolUseFailure if error else HookEvent.PostToolUse
        post_payload = HookPayload(
            event=post_event,
            tool_name=name,
            tool_args=args,
            tool_result=result,
            error=error,
        )
        hook_runner.run(post_event, post_payload)

    # L1 Tool-Result Budget：大输出落盘
    if budget and isinstance(budget, ToolResultBudget) and workspace and not error:
        result = budget.apply(result, name, workspace)

    return _tm(result if isinstance(result, dict) else {"ok": True, "result": result})


# ---------------------------------------------------------------------------
# 工具错误归一化
# ---------------------------------------------------------------------------

def normalize_tool_error(tool_name: str, result: Any) -> dict[str, Any] | None:
    """从工具结果中提取错误信息"""
    if not isinstance(result, dict) or result.get("ok", True):
        return None
    reason = str(result.get("error_message") or result.get("error") or "tool failed")
    return {
        "tool": tool_name,
        "reason": reason,
        "recoverable": bool(result.get("recoverable", True)),
        "suggested_fix": str(result.get("suggested_fix") or _default_tool_error_fix(tool_name, reason)),
    }


def _default_tool_error_fix(tool_name: str, reason: str) -> str:
    lowered = reason.lower()
    if "not been read" in lowered or "read it" in lowered:
        return "Read the file again before editing or overwriting it."
    if "permission" in lowered or "denied" in lowered or "blocked" in lowered:
        return "Ask for approval or choose an allowed, safer alternative."
    if "timeout" in lowered:
        return "Retry with a narrower command or increase the timeout if appropriate."
    if "does not exist" in lowered or "not found" in lowered:
        return "Verify the path or create the missing prerequisite first."
    return f"Inspect the {tool_name} result, fix the cause, then retry if needed."
