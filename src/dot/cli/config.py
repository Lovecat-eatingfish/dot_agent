"""
CLI 配置加载模块 — 合并 .env + yaml + 默认值

加载优先级：os.environ（含 .env） > yaml > 代码默认值
配置文件路径：.dot/config.yaml（与 .dot/mcp.json 同级）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ..core.log import get_logger

logger = get_logger(__name__)

# 必需配置项（缺失时给出友好提示，不阻断启动）
_REQUIRED_KEYS: dict[str, str] = {
    "api_key": "API_KEY — 请在 .env 文件中设置 API_KEY=your-key",
}

# 默认配置
_DEFAULTS: dict[str, Any] = {
    "model": "gpt-4o",
    "base_url": "https://api.openai.com/v1",
    "context_window": 128000,
    "log_level": "INFO",
}

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass
class CLIConfig:
    """运行时配置容器，合并 .env + yaml + 默认值"""

    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-4o"
    context_window: int = 128000
    log_level: str = "INFO"
    workspace: Path = field(default_factory=Path.cwd)
    mcp_config: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, workspace: Path | str | None = None) -> CLIConfig:
        """加载配置：env > yaml > defaults"""
        # 1. 加载 .env（不覆盖已有环境变量）
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            logger.debug("[config] loaded .env from %s", env_path)

        # 2. 加载 yaml 配置
        yaml_data: dict[str, Any] = {}
        yaml_path = Path.cwd() / ".dot" / "config.yaml"
        if yaml_path.exists():
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f) or {}
                logger.debug("[config] loaded yaml from %s", yaml_path)
            except Exception as exc:
                logger.warning("[config] failed to load yaml: %s", exc)

        # 3. 合并：env > yaml > defaults
        ws = Path(workspace).expanduser() if workspace else Path.cwd()

        return cls(
            api_key=os.environ.get("API_KEY") or yaml_data.get("api_key"),
            base_url=os.environ.get("BASE_URL") or yaml_data.get("base_url", _DEFAULTS["base_url"]),
            model=os.environ.get("MODEL") or yaml_data.get("model", _DEFAULTS["model"]),
            context_window=int(
                os.environ.get("CONTEXT_WINDOW")
                or yaml_data.get("context_window", _DEFAULTS["context_window"])
            ),
            log_level=os.environ.get("DOT_LOG_LEVEL") or yaml_data.get("log_level", _DEFAULTS["log_level"]),
            workspace=ws,
            mcp_config=yaml_data.get("mcp", {}),
            extra={k: v for k, v in yaml_data.items() if k not in {
                "api_key", "base_url", "model", "context_window", "log_level", "mcp"
            }},
        )

    def validate(self) -> list[str]:
        """校验配置，返回警告列表（不阻断启动）"""
        warnings: list[str] = []
        for key, hint in _REQUIRED_KEYS.items():
            val = getattr(self, key, None)
            if not val:
                warnings.append(f"配置缺失: {hint}")
        if self.log_level.upper() not in _VALID_LOG_LEVELS:
            warnings.append(f"log_level 无效: {self.log_level}（有效值: {', '.join(_VALID_LOG_LEVELS)}）")
        return warnings

    def masked(self) -> dict[str, Any]:
        """返回配置 dict，敏感信息脱敏"""
        api_key = self.api_key or ""
        if len(api_key) > 12:
            masked_key = api_key[:6] + "***" + api_key[-4:]
        elif api_key:
            masked_key = "***"
        else:
            masked_key = "(未设置)"
        return {
            "model": self.model,
            "base_url": self.base_url or "(未设置)",
            "api_key": masked_key,
            "context_window": self.context_window,
            "log_level": self.log_level,
            "workspace": str(self.workspace),
        }

    def set(self, key: str, value: str) -> None:
        """运行时修改配置项"""
        if hasattr(self, key):
            current = getattr(self, key)
            # 类型转换
            if isinstance(current, int):
                try:
                    value = int(value)  # type: ignore[assignment]
                except ValueError:
                    pass
            elif isinstance(current, Path):
                value = Path(value)  # type: ignore[assignment]
            setattr(self, key, value)
            logger.info("[config] set %s = %s", key, value)
        else:
            self.extra[key] = value
            logger.info("[config] set extra.%s = %s", key, value)
