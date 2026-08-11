"""
工具调用重试机制

为工具调用添加指数退避重试，提高可靠性。
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class RetryableError(Exception):
    """可重试的错误"""
    pass


class NonRetryableError(Exception):
    """不可重试的错误"""
    pass


def retry_on_failure(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    retryable_errors: tuple[type[Exception], ...] = (RetryableError,),
):
    """工具调用重试装饰器（指数退避）

    Args:
        max_attempts: 最大重试次数
        initial_delay: 初始延迟（秒）
        max_delay: 最大延迟（秒）
        exponential_base: 指数基数
        retryable_errors: 可重试的异常类型

    Example:
        @retry_on_failure(max_attempts=3)
        def my_tool_function(...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            delay = initial_delay

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_errors as exc:
                    last_exception = exc
                    if attempt < max_attempts:
                        logger.warning(
                            "%s attempt %d/%d failed: %s. Retrying in %.1fs...",
                            func.__name__,
                            attempt,
                            max_attempts,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                        delay = min(delay * exponential_base, max_delay)
                    else:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__,
                            max_attempts,
                            exc,
                        )
                except Exception as exc:
                    # 不可重试的异常，直接抛出
                    logger.error("%s failed with non-retryable error: %s", func.__name__, exc)
                    raise

            # 所有重试都失败
            return {
                "ok": False,
                "error": "max_retries_exceeded",
                "error_message": f"Failed after {max_attempts} attempts: {last_exception}",
                "tool": func.__name__,
                "hint": "Check network connection or try again later",
            }

        return wrapper
    return decorator


def invoke_tool_with_retry(
    tool_func: Callable,
    tool_input: dict[str, Any],
    *,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    exponential_base: float = 2.0,
) -> Any:
    """调用工具并自动重试

    Args:
        tool_func: 工具函数
        tool_input: 工具输入
        max_attempts: 最大重试次数
        initial_delay: 初始延迟（秒）
        exponential_base: 指数基数

    Returns:
        工具执行结果
    """
    last_error = None
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            result = tool_func(**tool_input)
            # 检查结果是否表示可重试的错误
            if isinstance(result, dict) and result.get("ok") is False:
                error_type = result.get("error", "")
                # 这些错误可以重试
                if error_type in ("timeout", "network_error", "rate_limited", "temporary_failure"):
                    last_error = result
                    if attempt < max_attempts:
                        logger.warning(
                            "Tool call attempt %d/%d failed with retryable error: %s. Retrying in %.1fs...",
                            attempt,
                            max_attempts,
                            error_type,
                            delay,
                        )
                        time.sleep(delay)
                        delay = delay * exponential_base
                        continue
            return result
        except Exception as exc:
            last_error = {"error": str(exc)}
            if attempt < max_attempts:
                logger.warning(
                    "Tool call attempt %d/%d failed with exception: %s. Retrying in %.1fs...",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = delay * exponential_base
                continue
            raise

    # 所有重试都失败
    return {
        "ok": False,
        "error": "max_retries_exceeded",
        "error_message": f"Failed after {max_attempts} attempts",
        "last_error": last_error,
    }
