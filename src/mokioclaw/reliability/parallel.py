"""
并行工具调用支持

对齐 Claude Code StreamingToolExecutor：
- is_concurrency_safe=True（只读）→ 可并行
- 任意 mutating 工具（Edit/Write/Bash/...）→ 整批串行，避免文件竞态
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


def are_tools_independent(tool_calls: list[dict[str, Any]]) -> bool:
    """判断一组工具调用是否可以并行执行

    两层检查：
    1. 路径级冲突检测：写/读同一文件 → 不独立
    2. 注册表并发安全标记：全部 concurrency_safe → 独立
    """
    if len(tool_calls) <= 1:
        return False

    # ── 第一层：文件路径冲突检测 ──
    write_paths: list[str] = []
    read_paths: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args = tc.get("args", {})
        if name in ("FileWriteTool", "FileEditTool"):
            write_paths.append(str(args.get("file_path", "")))
        elif name == "FileReadTool":
            read_paths.append(str(args.get("file_path", "")))

    # 重复写路径 → 不独立
    if len(write_paths) != len(set(write_paths)):
        return False

    # 读写重叠 → 不独立
    if write_paths and read_paths and set(write_paths) & set(read_paths):
        return False

    # ── 第二层：注册表并发安全标记 ──
    from mokioclaw.tools.registry import is_tool_concurrency_safe

    names = [tc.get("name", "") for tc in tool_calls]
    return all(is_tool_concurrency_safe(n) for n in names)


def execute_tools_in_parallel(
    tool_calls: list[dict[str, Any]],
    execute_tool_func: Callable[[dict[str, Any]], Any],
    *,
    max_workers: int = 4,
) -> list[Any]:
    """并行执行独立的工具调用；不安全时退化为串行。

    对齐 Claude Code StreamingToolExecutor：BashTool 失败时级联取消未完成的兄弟工具。
    """
    if not tool_calls:
        return []

    if len(tool_calls) == 1 or not are_tools_independent(tool_calls):
        return [execute_tool_func(tc) for tc in tool_calls]

    logger.info("Executing %d tools in parallel", len(tool_calls))

    results: list[Any] = [None] * len(tool_calls)
    sibling_abort = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(execute_tool_func, tc): i
            for i, tc in enumerate(tool_calls)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            if sibling_abort and results[index] is None:
                # 尽量取消尚未开始的 future
                future.cancel()
                results[index] = {
                    "ok": False,
                    "error": "cancelled: sibling BashTool failed",
                    "tool": tool_calls[index].get("name", "unknown"),
                    "sibling_aborted": True,
                }
                continue
            try:
                results[index] = future.result()
            except Exception as exc:
                logger.error("Tool call %d failed: %s", index, exc)
                results[index] = {
                    "ok": False,
                    "error": str(exc),
                    "tool": tool_calls[index].get("name", "unknown"),
                }
            # Bash 失败 → 级联 abort（对齐 StreamingToolExecutor.hasErrored）
            if _is_bash_failure(tool_calls[index], results[index]):
                sibling_abort = True
                logger.warning("BashTool failed; cascading abort to sibling tools")
                for pending, idx in list(future_to_index.items()):
                    if results[idx] is None and not pending.done():
                        pending.cancel()
                        results[idx] = {
                            "ok": False,
                            "error": "cancelled: sibling BashTool failed",
                            "tool": tool_calls[idx].get("name", "unknown"),
                            "sibling_aborted": True,
                        }

    # 填补被 cancel 但仍为 None 的槽位
    for i, result in enumerate(results):
        if result is None:
            results[i] = {
                "ok": False,
                "error": "cancelled: sibling BashTool failed",
                "tool": tool_calls[i].get("name", "unknown"),
                "sibling_aborted": True,
            }
    return results


def _is_bash_failure(tool_call: dict[str, Any], result: Any) -> bool:
    name = str(tool_call.get("name") or "")
    if name != "BashTool":
        return False
    if isinstance(result, dict):
        return result.get("ok") is False or result.get("is_error") is True
    # ToolMessage / tuple 包装
    content = getattr(result, "content", None)
    if content is None and isinstance(result, tuple) and result:
        content = getattr(result[0], "content", None)
    if isinstance(content, str) and '"ok": false' in content.lower().replace(" ", ""):
        return True
    if isinstance(content, dict) and content.get("ok") is False:
        return True
    return False


async def execute_tools_in_parallel_async(
    tool_calls: list[dict[str, Any]],
    execute_tool_func: Callable[[dict[str, Any]], Any],
    *,
    max_concurrency: int = 4,
) -> list[Any]:
    """异步并行执行工具调用"""
    if not tool_calls:
        return []

    if len(tool_calls) == 1 or not are_tools_independent(tool_calls):
        return [await execute_tool_func(tc) for tc in tool_calls]

    semaphore = asyncio.Semaphore(max_concurrency)

    async def execute_with_semaphore(tc: dict[str, Any]) -> Any:
        async with semaphore:
            return await execute_tool_func(tc)

    tasks = [execute_with_semaphore(tc) for tc in tool_calls]
    return await asyncio.gather(*tasks, return_exceptions=False)
