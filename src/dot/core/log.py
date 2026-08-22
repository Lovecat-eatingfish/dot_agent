"""
统一日志配置（dot 独立副本）

默认输出到 stderr（控制台调试可见），DEBUG 级别。
可通过 DOT_LOG_LEVEL / MOKIO_LOG_LEVEL 环境变量调整。
"""
from __future__ import annotations

import logging
import os
import sys
from dotenv import load_dotenv

# ✅ 模块导入就加载 .env
load_dotenv()

_initialized = False


def _env(name: str, legacy_name: str) -> str | None:
    return os.environ.get(name) or os.environ.get(legacy_name)


def setup_logging(level: str | None = None) -> None:
    """初始化日志配置，输出到 stderr

    Args:
        level: 日志级别字符串，None 时读环境变量，默认 DEBUG（控制台调试模式）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    resolved = (level or _env("DOT_LOG_LEVEL", "MOKIO_LOG_LEVEL") or "DEBUG").upper()
    log_level = getattr(logging, resolved, logging.DEBUG)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    # ✅ 使用 stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    # ✅ 安全删除，不要直接 clear()
    for h in root.handlers[:]:
        root.removeHandler(h)

    root.addHandler(handler)
    root.setLevel(log_level)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "uvicorn", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取 logger 实例"""
    return logging.getLogger(name)
