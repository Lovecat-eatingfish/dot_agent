"""
LLM Provider（dot 独立副本）

提供 ChatOpenAI 模型实例的统一创建入口。
支持重试、超时、环境变量校验、模型降级回退。

环境变量（.env）：
  API_KEY / MODEL / BASE_URL          — 必需
  MODEL_FALLBACKS                     — 可选，逗号分隔降级模型列表
  DOT_REQUEST_TIMEOUT / MOKIO_REQUEST_TIMEOUT — 请求超时（秒），默认 120
  DOT_MAX_RETRIES / MOKIO_MAX_RETRIES         — 重试次数，默认 3
"""
from __future__ import annotations

import os
import threading
from typing import Any

import dotenv

from langchain_openai import ChatOpenAI

from .log import get_logger

logger = get_logger(__name__)

# 默认请求超时（秒）
DEFAULT_REQUEST_TIMEOUT = 120
# 默认重试次数
DEFAULT_MAX_RETRIES = 3

# 环境变量缓存，避免每次 create_model() 都重新校验
_validated_env: dict[str, str] | None = None
_env_lock = threading.Lock()

# 运行时模型覆盖（进程级）
_active_model_override: str | None = None
_override_lock = threading.Lock()


def set_active_model(name: str | None) -> None:
    """设置进程内模型覆盖；空字符串/None 清除覆盖（回落 env 默认）"""
    global _active_model_override
    with _override_lock:
        _active_model_override = (name or "").strip() or None


def get_active_model() -> str | None:
    """读取当前覆盖的模型名（None=未覆盖，用 env 默认）"""
    with _override_lock:
        return _active_model_override


def validate_env() -> dict[str, str]:
    """校验并返回必需的环境变量，只在首次调用时执行（双重检查锁定）。"""
    global _validated_env
    if _validated_env is not None:
        return _validated_env

    with _env_lock:
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

        _validated_env = {
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
        }
        return _validated_env


def create_model(
    *,
    model: str | None = None,
    request_timeout: int | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """创建 ChatOpenAI 模型实例

    Args:
        model: 覆盖 env 的 MODEL；None 时用 env 默认模型（降级回退时用）。
        request_timeout: 请求超时秒数，默认读 DOT_REQUEST_TIMEOUT 环境变量
        max_retries: 最大重试次数，默认读 DOT_MAX_RETRIES 环境变量
    """
    env = validate_env()

    timeout = request_timeout or _env_int("DOT_REQUEST_TIMEOUT", "MOKIO_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
    retries = max_retries if max_retries is not None else _env_int("DOT_MAX_RETRIES", "MOKIO_MAX_RETRIES", DEFAULT_MAX_RETRIES)

    # 模型优先级：显式参数（降级回退用）> 运行时覆盖 > env 默认
    resolved_model = model or get_active_model() or env["model"]
    # ===== 加这一段打印 =====
    print("=" * 50)
    print("[DEBUG] LLM 调用参数：")
    print(f"  model     : {resolved_model}")
    print(f"  base_url  : {env['base_url']}")
    print(f"  api_key   : {env['api_key'][:8]}...{env['api_key'][-4:]}")  # 只打印前后几位，防泄露
    print("=" * 50)
    return ChatOpenAI(
        api_key=env["api_key"],
        openai_api_key=env["api_key"],
        model=resolved_model,
        base_url=env["base_url"],
        temperature=0,
        request_timeout=timeout,
        max_retries=retries,
    )


def _fallback_models() -> list[str]:
    """读取 MODEL_FALLBACKS 环境变量（逗号分隔），返回降级模型列表。"""
    raw = os.getenv("MODEL_FALLBACKS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


class FallbackTriggeredError(RuntimeError):
    """所有降级模型都失败时抛出"""


def invoke_with_fallback(
    model: ChatOpenAI,
    messages: list,
    *,
    fallbacks: list[str] | None = None,
) -> Any:
    """调用模型，主模型失败（非 400 类参数错误）时降级到 fallback 模型

    语义：
    - 主模型抛异常 → 尝试 fallback 列表里的下一个模型
    - BadRequestError（400，参数/上下文问题）不降级直接抛（降级也救不了）
    - 全部 fallback 都失败 → 抛 FallbackTriggeredError
    """
    fallback_list = fallbacks if fallbacks is not None else _fallback_models()
    try:
        return model.invoke(messages)
    except Exception as exc:
        if _is_bad_request(exc):
            raise
        if not fallback_list:
            raise
        logger.warning("primary model failed (%s), falling back to %s", type(exc).__name__, fallback_list)
        # 从主模型提取已绑定的 tools，传递给降级模型（否则降级模型无 tool_calls，循环立即退出）
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
                    raise
                logger.error("fallback model %s failed: %s", fb_model_name, type(exc2).__name__, exc_info=True)
                continue
        raise FallbackTriggeredError(
            f"all models failed (primary + {len(fallback_list)} fallbacks): {last_exc}"
        ) from last_exc


def _extract_bound_tools(model: Any) -> list[Any]:
    """从已绑定工具的模型实例提取工具列表（bind_tools 存储在 model.kwargs['tools']）"""
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
    return any(m in text for m in ("context_length", "context window", "maximum context length"))


def _env_int(name: str, legacy_name: str, default: int) -> int:
    raw = os.environ.get(name) or os.environ.get(legacy_name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def reset_env_cache() -> None:
    """重置环境变量缓存（仅用于测试）"""
    global _validated_env
    with _env_lock:
        _validated_env = None
