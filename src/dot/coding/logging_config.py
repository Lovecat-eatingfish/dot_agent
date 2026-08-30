"""
dot.coding.logging_config — 集中日志配置

一次调用 setup()，全局生效：
  - stderr 输出彩色日志（console 调试用）
  - .dot/logs/dot.log 落盘，10MB 自动轮转
  - 所有已有 logger = logging.getLogger(__name__) 自动生效
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def setup(
    workspace: Path | None = None,
    level: str = "INFO",
    console: bool = True,
    file: bool = True,
    file_max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    file_backup_count: int = 5,
) -> None:
    """配置全局日志

    Args:
        workspace: 日志文件存放的 workspace 根目录（默认 cwd）
        level: 日志级别 DEBUG / INFO / WARNING / ERROR
        console: 是否输出到 stderr
        file: 是否输出到 .dot/logs/dot.log
        file_max_bytes: 单个日志文件最大字节数
        file_backup_count: 保留的轮转文件数
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复配置
    if root.handlers:
        return

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(fmt)
        root.addHandler(handler)

    if file:
        log_dir = (workspace or Path.cwd()) / ".dot" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "dot.log",
            maxBytes=file_max_bytes,
            backupCount=file_backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)

    # 降低第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
