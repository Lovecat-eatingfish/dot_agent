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

REPL/TUI 场景使用 quiet=True（不写 stderr，由 SidebarLogHandler 统一捕获）。
"""
from __future__ import annotations

import io
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
# REPL 静默模式时替换的 stderr 缓冲（供恢复用）
_quiet_buffer: io.StringIO | None = None
_old_stderr: object | None = None


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
        if sys.stderr.isatty():
            return f"{color}{msg}{AnsiColor.RESET}"
        return msg


def _env(name: str, legacy_name: str) -> str | None:
    return os.environ.get(name) or os.environ.get(legacy_name)


def enter_quiet_mode() -> None:
    """切换到静默模式：stderr 重定向到缓冲区（REPL 启动前调用）"""
    global _quiet_buffer, _old_stderr
    if _quiet_buffer is not None:
        return
    _old_stderr = sys.stderr
    _quiet_buffer = io.StringIO()
    sys.stderr = _quiet_buffer


def exit_quiet_mode() -> None:
    """恢复 stderr（REPL 退出时调用）"""
    global _quiet_buffer, _old_stderr
    if _quiet_buffer is None:
        return
    sys.stderr = _old_stderr
    _quiet_buffer = None
    _old_stderr = None


def setup_logging(level: str | None = None, quiet: bool = False) -> None:
    """初始化日志配置

    Args:
        quiet: True 时 stderr 重定向到缓冲区（REPL/TUI 使用）；False 时输出到终端。
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    resolved = (level or _env("DOT_LOG_LEVEL", "MOKIO_LOG_LEVEL") or "DEBUG").upper()
    log_level = getattr(logging, resolved, logging.DEBUG)

    # 静默模式：捕获所有日志但不污染终端
    if quiet:
        enter_quiet_mode()

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
