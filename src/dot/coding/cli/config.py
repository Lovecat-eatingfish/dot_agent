"""
dot.coding.cli.config — CLI 配置加载

合并 .env + yaml + 默认值。
加载优先级：os.environ > yaml > 代码默认值
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_DEFAULTS: dict[str, Any] = {
    "model": "gpt-4o",
    "base_url": "https://api.openai.com/v1",
    "context_window": 128000,
    "log_level": "INFO",
}


@dataclass
class CLIConfig:
    """运行时配置容器"""
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
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        yaml_data: dict[str, Any] = {}
        yaml_path = Path.cwd() / ".dot" / "config.yaml"
        if yaml_path.exists():
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f) or {}
            except Exception:
                pass

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
        )

    def validate(self) -> list[str]:
        warnings: list[str] = []
        if not self.api_key:
            warnings.append("配置缺失: API_KEY — 请在 .env 文件中设置")
        return warnings

    def masked(self) -> dict[str, Any]:
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
            "workspace": str(self.workspace),
        }
