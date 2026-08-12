"""
并行工具调用支持

识别独立的工具调用并并行执行，提高执行效率。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


def are_tools_independent(tool_calls: list[dict[str, Any]]) -> bool:
    """判断一组工具调用是否可以并行执行

    独立的工具调用满足以下条件：
    1. 不依赖其他工具调用的结果
    2. 不读取相同的文件（会冲突）
    3. 不修改相同的文件（会冲突）

    Args:
        tool_calls: 工具调用列表

    Returns:
        是否可以并行执行
    """
    if len(tool_calls) <= 1:
        return False

    # 检查是否有写操作
    has_write = any(
        tc.get("name", "") in ("FileWriteTool", "FileEditTool")
        for tc in tool_calls
    )

    # 收集所有文件路径（用于检测冲突）
    write_paths = [
        tc.get("args", {}).get("file_path", "")
        for tc in tool_calls
        if tc.get("name", "") in ("FileWriteTool", "FileEditTool")
    ]
    read_paths = [
        tc.get("args", {}).get("file_path", "")
        for tc in tool_calls
        if tc.get("name", "") == "FileReadTool"
    ]

    # 检查是否有重复的文件路径（写操作）
    if len(write_paths) != len(set(write_paths)):
        # 有重复的写路径，不能并行
        return False

    # 检查写操作和读操作是否有重叠
    if write_paths and read_paths:
        if set(write_paths) & set(read_paths):
            # 读写同一个文件，不能并行
            return False

    # 检查是否有写操作和读操作混合
    has_read = len(read_paths) > 0
    if has_write and has_read and not (set(write_paths) & set(read_paths)):
        # 有写有读，但目标文件不同，可以并行
        return True

    # 只有读操作，可以并行
    if not has_write:
        return True

    # 只有写操作，且目标文件不重复
    return len(write_paths) == len(set(write_paths))


def execute_tools_in_parallel(
    tool_calls: list[dict[str, Any]],
    execute_tool_func: Callable[[dict[str, Any]], Any],
    *,
    max_workers: int = 4,
) -> list[Any]:
    """并行执行独立的工具调用

    Args:
        tool_calls: 工具调用列表
        execute_tool_func: 执行单个工具调用的函数
        max_workers: 最大并发数

    Returns:
        工具执行结果列表（顺序与输入一致）
    """
    if not tool_calls:
        return []

    if len(tool_calls) == 1 or not are_tools_independent(tool_calls):
        # 串行执行
        return [execute_tool_func(tc) for tc in tool_calls]

    # 并行执行
    logger.info("Executing %d tools in parallel", len(tool_calls))

    results: list[Any] = [None] * len(tool_calls)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_index = {
            executor.submit(execute_tool_func, tc): i
            for i, tc in enumerate(tool_calls)
        }

        # 收集结果
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                logger.error("Tool call %d failed: %s", index, exc)
                results[index] = {
                    "ok": False,
                    "error": str(exc),
                    "tool": tool_calls[index].get("name", "unknown"),
                }

    return results


async def execute_tools_in_parallel_async(
    tool_calls: list[dict[str, Any]],
    execute_tool_func: Callable[[dict[str, Any]], Any],
    *,
    max_concurrency: int = 4,
) -> list[Any]:
    """异步并行执行工具调用

    Args:
        tool_calls: 工具调用列表
        execute_tool_func: 异步执行函数
        max_concurrency: 最大并发数

    Returns:
        工具执行结果列表
    """
    if not tool_calls:
        return []

    if len(tool_calls) == 1 or not are_tools_independent(tool_calls):
        # 串行执行
        return [await execute_tool_func(tc) for tc in tool_calls]

    # 使用信号量限制并发
    semaphore = asyncio.Semaphore(max_concurrency)

    async def execute_with_semaphore(tc: dict[str, Any]) -> Any:
        async with semaphore:
            return await execute_tool_func(tc)

    tasks = [execute_with_semaphore(tc) for tc in tool_calls]
    return await asyncio.gather(*tasks, return_exceptions=False)
