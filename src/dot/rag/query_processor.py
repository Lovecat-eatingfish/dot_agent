"""
dot.rag.query_processor — 查询理解层（M1：会话感知改写）

多轮会话中的追问（"第二个阶段具体怎么做？"）脱离上下文后无法独立检索，
这里用 LLM 把"历史对话 + 当前问句"改写为独立完整的检索问句。
改写只影响检索输入，呈现给 LLM 的仍是用户原话。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from dot.ai.events import ProviderEvent, TextDeltaEvent
from dot.ai.providers.openai import OpenAIProvider
from dot.ai.types import UserMessage

from .config import RagSettings

REWRITE_SYSTEM = """你是一个检索问句改写器。根据对话历史，把用户的最新问题改写为一个
独立、完整、不依赖上下文就能理解的检索问句。

要求：
- 只输出改写后的问句本身，不要任何解释、引号或前缀
- 补全指代（如"它""第二个阶段"）为历史中的具体对象
- 不要扩展问题范围，不要添加历史中没有的信息
- 如果问题本身已经独立完整，原样输出"""


def build_history_text(history: list) -> str:
    """把 dot.ai 消息对象压成纯文本对话记录"""
    lines = []
    for m in history:
        role = "用户" if m.role == "user" else "助手"
        lines.append(f"{role}：{m.text}")
    return "\n".join(lines)


async def rewrite_with_history(
    question: str, history: list, settings: RagSettings | None = None
) -> AsyncIterator[ProviderEvent]:
    """带历史的问句改写，流式产出 ProviderEvent"""
    settings = settings or RagSettings()
    provider = OpenAIProvider()
    system = REWRITE_SYSTEM
    user = f"对话历史：\n{build_history_text(history)}\n\n最新问题：{question}\n\n改写后的检索问句："
    async for event in provider.stream_response(
        model=settings.resolve_generate_model(),
        system=system,
        messages=[UserMessage(content=user)],
        tools=[],
    ):
        yield event


async def rewrite_query(question: str, history: list, settings: RagSettings | None = None) -> str:
    """便捷封装：返回改写后的问句（失败时退回原问句）"""
    parts: list[str] = []
    try:
        async for event in rewrite_with_history(question, history, settings):
            if isinstance(event, TextDeltaEvent):
                parts.append(event.delta)
        rewritten = "".join(parts).strip().strip('"“”')
        return rewritten or question
    except Exception:
        return question
