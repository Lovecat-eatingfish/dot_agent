"""
dot.ai.catalog — Provider 配置目录

内置目录打包在代码中（data/catalog.toml），
用户目录（~/.dot/catalog.toml）覆盖内置。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ModelConfig:
    """单个模型配置"""
    context_window: int = 128000
    max_output: int = 4096
    supports_tools: bool = True
    supports_thinking: bool = False


@dataclass
class ProviderConfig:
    """单个 Provider 配置"""
    name: str
    type: str  # "openai" | "anthropic" | "openai-compatible"
    base_url: str
    api_key_env: str = "API_KEY"
    models: dict[str, ModelConfig] = field(default_factory=dict)


@dataclass
class ProviderCatalog:
    """Provider 配置目录

    加载顺序：
    1. 内置目录（src/dot/ai/data/catalog.toml）
    2. 用户目录（~/.dot/catalog.toml）覆盖内置
    """
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, user_dir: Path | None = None) -> ProviderCatalog:
        """加载配置目录

        Args:
            user_dir: 用户配置目录，默认 ~/.dot/
        """
        catalog = cls()

        # 1. 加载内置目录
        builtin_path = Path(__file__).parent / "data" / "catalog.toml"
        if builtin_path.is_file():
            catalog._load_file(builtin_path)

        # 2. 加载用户目录（覆盖）
        if user_dir is None:
            user_dir = Path.home() / ".dot"
        user_path = user_dir / "catalog.toml"
        if user_path.is_file():
            catalog._load_file(user_path)

        return catalog

    def _load_file(self, path: Path) -> None:
        """从 TOML 文件加载配置"""
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[catalog] %s 加载失败: %s", path, exc,
            )
            return

        providers_data = data.get("providers", {})
        if not isinstance(providers_data, dict):
            return

        for name, pconf in providers_data.items():
            if not isinstance(pconf, dict):
                continue

            provider = ProviderConfig(
                name=name,
                type=pconf.get("type", "openai-compatible"),
                base_url=pconf.get("base_url", ""),
                api_key_env=pconf.get("api_key_env", "API_KEY"),
            )

            models_data = pconf.get("models", {})
            if isinstance(models_data, dict):
                for model_name, mconf in models_data.items():
                    if not isinstance(mconf, dict):
                        continue
                    provider.models[model_name] = ModelConfig(
                        context_window=mconf.get("context_window", 128000),
                        max_output=mconf.get("max_output", 4096),
                        supports_tools=mconf.get("supports_tools", True),
                        supports_thinking=mconf.get("supports_thinking", False),
                    )

            self.providers[name] = provider

    def get_provider(self, name: str) -> ProviderConfig | None:
        """获取 Provider 配置"""
        return self.providers.get(name)

    def get_model(self, provider_name: str, model_name: str) -> ModelConfig | None:
        """获取模型配置"""
        provider = self.providers.get(provider_name)
        if provider is None:
            return None
        return provider.models.get(model_name)

    def get_context_window(self, provider_name: str, model_name: str) -> int:
        """获取模型上下文窗口大小（默认 128000）"""
        model = self.get_model(provider_name, model_name)
        return model.context_window if model else 128000
