"""
dot.rag.generator — 基于 dot.ai Provider 的生成端

复用项目现有的 OpenAI 兼容 Provider（.env 统一配置 API_KEY/BASE_URL/MODEL），
本地部署（Ollama / vLLM / LM Studio）或云端模型都只需要改 .env，RAG 代码零改动。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from dot.ai.events import ProviderEvent, TextDeltaEvent
from dot.ai.providers.openai import OpenAIProvider
from dot.ai.types import UserMessage

from .config import RagSettings

SYSTEM_PROMPT = """你是一个严谨的问答助手。请仅根据下面提供的参考上下文回答用户问题：
- 上下文中有答案时，用中文简洁回答，并指出依据来自哪篇文档。
- 上下文不足以回答时，明确说明"根据现有资料无法回答"，不要编造。

参考上下文：
{context}"""


def build_prompt(question: str, docs: list, settings: RagSettings | None = None) -> str:
    """把检索到的 chunk 拼进 system prompt"""
    context = "\n\n".join(
        f"【片段{i + 1}】（来源: {d.metadata.get('source', 'unknown')}）\n{d.page_content}"
        for i, d in enumerate(docs)
    )
    return SYSTEM_PROMPT.format(context=context)


async def generate_answer(
    question: str,
    docs: list,
    settings: RagSettings | None = None,
    provider: OpenAIProvider | None = None,
    system: str | None = None,
    history: list | None = None,
) -> AsyncIterator[ProviderEvent]:
    """带上下文调用 LLM，流式产出 ProviderEvent

    system：可传入预构建的 prompt；history：多轮会话历史（User/Assistant 消息）。
    """
    settings = settings or RagSettings()
    provider = provider or OpenAIProvider()
    if system is None:
        system = build_prompt(question, docs, settings)
    messages = list(history or [])
    messages.append(UserMessage(content=question))
    async for event in provider.stream_response(
        model=settings.resolve_generate_model(),
        system=system,
        messages=messages,
        tools=[],
    ):
        yield event


async def answer_text(question: str, docs: list, settings: RagSettings | None = None) -> str:
    """便捷封装：非流式拿完整回答文本"""
    chunks: list[str] = []
    async for event in generate_answer(question, docs, settings):
        if isinstance(event, TextDeltaEvent):
            chunks.append(event.delta)
    return "".join(chunks)
