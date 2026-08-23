"""
统一日志配置（dot 独立副本，移除 colorlog 依赖）

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


# ANSI 颜色码
class AnsiColor:
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD_PURPLE = "\033[1;35m"
    RESET = "\033[0m"


load_dotenv()

_initialized = False


class ColoredFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: AnsiColor.CYAN,
        logging.INFO: AnsiColor.GREEN,
        logging.WARNING: AnsiColor.YELLOW,
        logging.ERROR: AnsiColor.RED,
        logging.CRITICAL: AnsiColor.BOLD_PURPLE,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, "")
        msg = super().format(record)
        # 仅终端输出才加颜色；非TTY输出不输出ANSI码
        if sys.stderr.isatty():
            return f"{color}{msg}{AnsiColor.RESET}"
        return msg


def _env(name: str, legacy_name: str) -> str | None:
    return os.environ.get(name) or os.environ.get(legacy_name)


def setup_logging(level: str | None = None) -> None:
    """初始化日志配置，输出到 stderr，带控制台彩色（纯标准库）"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    resolved = (level or _env("DOT_LOG_LEVEL", "MOKIO_LOG_LEVEL") or "DEBUG").upper()
    log_level = getattr(logging, resolved, logging.DEBUG)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(ColoredFormatter(fmt=fmt, datefmt=datefmt))

    root = logging.getLogger()
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
