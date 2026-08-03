"""
LLM Provider 模块

提供 ChatOpenAI 模型实例的统一创建入口。
支持重试、超时、环境变量校验。
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

# 默认请求超时（秒）
DEFAULT_REQUEST_TIMEOUT = 120
# 默认重试次数
DEFAULT_MAX_RETRIES = 3

# 环境变量缓存，避免每次 create_model() 都重新校验
_validated_env: dict[str, str] | None = None


def _validate_env() -> dict[str, str]:
    """校验并返回必需的环境变量，只在首次调用时执行"""
    global _validated_env
    if _validated_env is not None:
        return _validated_env

    api_key = os.getenv("API_KEY")
    model = os.getenv("MODEL")
    base_url = os.getenv("BASE_URL")

    missing = [
        name
        for name, value in {"API_KEY": api_key, "MODEL": model, "BASE_URL": base_url}.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing required .env setting(s): {', '.join(missing)}")

    _validated_env = {"api_key": api_key, "model": model, "base_url": base_url}
    return _validated_env


def create_model(
    *,
    request_timeout: int | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """创建 ChatOpenAI 模型实例

    Args:
        request_timeout: 请求超时秒数，默认从 MOKIO_REQUEST_TIMEOUT 环境变量读取
        max_retries: 最大重试次数，默认从 MOKIO_MAX_RETRIES 环境变量读取

    Returns:
        配置好的 ChatOpenAI 实例
    """
    env = _validate_env()

    timeout = request_timeout or _env_int("MOKIO_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
    retries = max_retries if max_retries is not None else _env_int("MOKIO_MAX_RETRIES", DEFAULT_MAX_RETRIES)

    return ChatOpenAI(
        api_key=env["api_key"],
        openai_api_key=env["api_key"],
        model=env["model"],
        base_url=env["base_url"],
        temperature=0,
        request_timeout=timeout,
        max_retries=retries,
    )


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def reset_env_cache() -> None:
    """重置环境变量缓存（仅用于测试）"""
    global _validated_env
    _validated_env = None
