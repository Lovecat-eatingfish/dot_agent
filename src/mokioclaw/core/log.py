"""
统一日志配置

所有模块通过 get_logger(__name__) 获取 logger，日志级别和输出目标
由环境变量控制：
- MOKIO_LOG_LEVEL: DEBUG / INFO / WARNING / ERROR / CRITICAL（默认 WARNING）
- MOKIO_LOG_FILE: 日志文件路径（默认只输出 stderr）

在 cli/app.py 入口处调用 setup_logging() 初始化一次即可。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_initialized = False


def setup_logging() -> None:
    """初始化全局日志配置，只应调用一次"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    level_name = os.environ.get("MOKIO_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)

    log_file = os.environ.get("MOKIO_LOG_FILE")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError:
            pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """获取 logger 实例

    用法：
        from mokioclaw.core.log import get_logger
        logger = get_logger(__name__)
        logger.warning("something happened", exc_info=True)
    """
    return logging.getLogger(name)
