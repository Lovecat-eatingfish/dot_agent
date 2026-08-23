"""
统一日志配置（dot 独立副本）

默认输出到 stderr（控制台调试可见），DEBUG 级别。
可通过 DOT_LOG_LEVEL / MOKIO_LOG_LEVEL 环境变量调整。
不同日志级别控制台区分颜色：
- DEBUG: 青色
- INFO: 绿色
- WARNING: 黄色
- ERROR: 红色
- CRITICAL: 紫红色
"""
from __future__ import annotations

import logging
import os
import sys
from dotenv import load_dotenv
import colorlog

# ✅ 模块导入就加载 .env
load_dotenv()

_initialized = False


def _env(name: str, legacy_name: str) -> str | None:
    return os.environ.get(name) or os.environ.get(legacy_name)


def setup_logging(level: str | None = None) -> None:
    """初始化日志配置，输出到 stderr，带控制台彩色"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    resolved = (level or _env("DOT_LOG_LEVEL", "MOKIO_LOG_LEVEL") or "DEBUG").upper()
    log_level = getattr(logging, resolved, logging.DEBUG)

    # 彩色格式
    # log_color: 自动根据级别替换颜色；asctime无颜色
    color_fmt = "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    # 颜色配置
    handler = colorlog.StreamHandler(stream=sys.stderr)
    handler.setFormatter(colorlog.ColoredFormatter(
        fmt=color_fmt,
        datefmt=datefmt,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_purple",
        }
    ))

    root = logging.getLogger()
    # ✅ 安全删除旧handler
    for h in root.handlers[:]:
        root.removeHandler(h)

    root.addHandler(handler)
    root.setLevel(log_level)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "uvicorn", "asyncio", "httpx2", "mcp.client.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取 logger 实例"""
    return logging.getLogger(name)
