"""
L1 关键事实提取 — 压缩后立即执行

从被裁剪的消息中提取关键事实，注入 system prompt 的 [Compression Context] 段。
设计约束（对齐设计文档）：
  - 提取：文件修改记录、关键决策、错误信息、用户偏好
  - 超时/失败：跳过 L1，仅做截断（fail-open）
  - 输出：结构化文本
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.log import get_logger
from ..core.llm import create_model
from ._utils import messages_to_text

logger = get_logger(__name__)

# L1 提取的最大 token 数（输入截断，避免提取本身超限）
MAX_INPUT_CHARS = 50_000

# L1 提取结果最大字符数
MAX_OUTPUT_CHARS = 3_000

L1_SYSTEM_PROMPT = """You are a context extraction assistant. Your job is to extract key facts from a conversation history that is about to be compressed.

Extract ONLY the following types of information:
1. **File Operations**: Which files were read, written, edited, or created. Include paths and brief descriptions of changes.
2. **Key Decisions**: Important technical decisions made during the conversation (architecture choices, library selections, etc.).
3. **Errors & Fixes**: Errors encountered and how they were resolved.
4. **User Preferences**: Any explicit user preferences or constraints mentioned.
5. **Task Progress**: What has been completed, what remains.

Rules:
- Output a concise structured summary in plain text.
- Use bullet points for readability.
- Maximum 500 words.
- Do NOT include conversation pleasantries or generic statements.
- Focus on information that would be useful for continuing the task.
- If there are no significant facts, output "No critical facts extracted."
"""

L1_USER_PROMPT = """Extract key facts from this conversation history:

{conversation}

Output the extracted facts as a structured summary."""


def extract_key_facts(messages: list[Any], session: Any = None) -> str:
    """从被裁剪的消息中提取关键事实

    Args:
        messages: 即将被裁剪的消息列表
        session: 可选 Session（用于链路追踪）

    Returns:
        提取的关键事实文本（空字符串表示提取失败或无关键事实）
    """
    if not messages:
        return ""

    # 预处理：转换为文本格式
    conversation_text = messages_to_text(messages)
    if not conversation_text.strip():
        return ""

    # 截断过长输入
    if len(conversation_text) > MAX_INPUT_CHARS:
        conversation_text = conversation_text[:MAX_INPUT_CHARS] + "\n... [truncated]"

    # 调用 LLM 提取
    try:
        model = create_model()
        response = model.invoke([
            SystemMessage(content=L1_SYSTEM_PROMPT),
            HumanMessage(content=L1_USER_PROMPT.format(conversation=conversation_text)),
        ])
        content = getattr(response, "content", "")
        if not content:
            logger.warning("[L1] LLM returned empty content")
            return ""

        # 截断过长输出
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n... [truncated]"

        logger.info("[L1] extracted %d chars of key facts", len(content))
        return content

    except Exception as exc:
        logger.warning("[L1] extraction failed (fail-open): %s", exc)
        return ""
