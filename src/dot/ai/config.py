"""
dot.ai.config — Provider 配置（Pydantic Settings）

使用 pydantic-settings 自动从环境变量 / .env 文件加载配置。
替代手动 os.environ.get()，支持：
  - 环境变量（API_KEY=sk-xxx）
  - .env 文件（项目根目录或用户目录）
  - 代码传入（api_key="sk-xxx" 优先级最高）
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
CURR = Path(__file__).parent
# 向上跳3层，回到项目根 dot_agent
PROJECT_ROOT = CURR.parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class OpenAISettings(BaseSettings):
    """OpenAI Provider 配置

    支持从 .env 文件或环境变量加载：
      API_KEY=sk-xxx
      BASE_URL=https://api.openai.com/v1/chat/completions
      MODEL=gpt-4o
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = 120.0

    def resolve_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "OpenAI API key not configured. "
                "Set the API_KEY environment variable, "
                "create a .env file with API_KEY=sk-xxx, "
                "or pass api_key= to OpenAIProvider()."
            )
        return self.api_key


# if __name__ == '__main__':
#     print(PROJECT_ROOT / ".env")

# settings = OpenAISettings()
# print(settings.base_url)
# print(settings.api_key)
# print(settings.model)
# print(settings.model_config)
# print(settings.resolve_api_key())
