"""
LLM Provider 模块

提供 ChatOpenAI 模型实例的统一创建入口。
支持重试、超时、环境变量校验、模型降级回退（对齐 Claude Code fallbackModel）。
"""
from __future__ import annotations

import os
from typing import Any

import dotenv

from langchain_openai import ChatOpenAI

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

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

    dotenv.load_dotenv()
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
    model: str | None = None,
    request_timeout: int | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """创建 ChatOpenAI 模型实例

    Args:
        model: 覆盖 env 的 MODEL；None 时用 env 默认模型。用于模型降级回退。
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
        model=model or env["model"],
        base_url=env["base_url"],
        temperature=0,
        request_timeout=timeout,
        max_retries=retries,
    )


def _fallback_models() -> list[str]:
    """读取 MODEL_FALLBACKS 环境变量（逗号分隔），返回降级模型列表

    对齐 Claude Code fallbackModel：主模型不可用时按列表顺序降级。
    """
    raw = os.getenv("MODEL_FALLBACKS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


class FallbackTriggeredError(RuntimeError):
    """所有降级模型都失败时抛出（对齐 Claude Code FallbackTriggeredError）"""


def invoke_with_fallback(
    model: ChatOpenAI,
    messages: list,
    *,
    fallbacks: list[str] | None = None,
) -> Any:  # noqa: F401 - 顶层 import 已有 Any 语义
    """调用模型，主模型失败（非 4xx 参数错误）时降级到 fallback 模型

    对齐 Claude Code fallbackModel 语义：
    - 主模型抛异常 → 尝试 fallback 列表里的下一个模型
    - 遇到 BadRequestError（400，参数/上下文问题）不降级，直接抛出（降级也救不了）
    - 全部 fallback 都失败 → 抛 FallbackTriggeredError

    Args:
        model: 主 ChatOpenAI 实例
        messages: 消息列表
        fallbacks: 降级模型名列表，None 时读 MODEL_FALLBACKS 环境变量
    """
    fallback_list = fallbacks if fallbacks is not None else _fallback_models()
    try:
        return model.invoke(messages)
    except Exception as exc:
        # 400 类错误（参数错误 / 上下文超长）降级也救不了，直接抛
        if _is_bad_request(exc):
            raise
        # 无降级模型 → 直接抛原异常
        if not fallback_list:
            raise
        logger.warning("primary model failed (%s), falling back to %s", type(exc).__name__, fallback_list)
        # 从主模型提取已绑定的 tools，传递给降级模型（否则降级模型无 tool_calls，循环立即退出 #3）
        bound_tools = _extract_bound_tools(model)
        last_exc = exc
        for fb_model_name in fallback_list:
            try:
                fb_model = create_model(model=fb_model_name)
                if bound_tools:
                    fb_model = fb_model.bind_tools(bound_tools)
                return fb_model.invoke(messages)
            except Exception as exc2:
                last_exc = exc2
                if _is_bad_request(exc2):
                    # 参数错误，继续降级无意义
                    raise
                logger.warning("fallback model %s failed: %s", fb_model_name, type(exc2).__name__)
                continue
        raise FallbackTriggeredError(
            f"all models failed (primary + {len(fallback_list)} fallbacks): {last_exc}"
        ) from last_exc


def _extract_bound_tools(model: Any) -> list:
    """从已 bind_tools 的模型实例提取 tools 列表

    LangChain 的 bind_tools 把 tools 存入 model.kwargs['tools']。
    降级模型需要复用这份 tools 才能发起工具调用。
    """
    kwargs = getattr(model, "kwargs", None)
    if not isinstance(kwargs, dict):
        return []
    tools = kwargs.get("tools")
    if isinstance(tools, list):
        return tools
    return []


def _is_bad_request(exc: Exception) -> bool:
    """判断是否为 400 类错误（参数 / 上下文超长），这类错误降级也救不了"""
    name = type(exc).__name__
    if name in {"BadRequestError"}:
        return True
    text = f"{name}: {exc}".lower()
    # 上下文超长、参数错误等
    return any(m in text for m in ("context_length", "context window", "maximum context length"))


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
