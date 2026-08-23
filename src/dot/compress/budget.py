"""
ContextBudgetAllocator — Token 预算计算

估算消息 token 数，计算各区域可用预算。
设计约束（对齐设计文档）：
  - 压缩后总 token ≤ 原始 context window 的 75%
  - 保守估算：1 token ≈ 3.5 chars（中文/英文混合）
"""
from __future__ import annotations

from typing import Any

from ..core.log import get_logger

logger = get_logger(__name__)

# 默认 context window（tokens）
DEFAULT_CONTEXT_WINDOW = 128_000

# token 估算常量（保守：中文 ≈ 2-3 chars/token，英文 ≈ 4 chars/token，取 3.5）
CHARS_PER_TOKEN = 3.5

# 预算分配比例（对齐设计文档）
SYSTEM_PROMPT_RATIO = 0.15      # system prompt 预留
TOOL_DEFINITIONS_RATIO = 0.10   # 工具定义预留
RESPONSE_RATIO = 0.15           # 模型回复预留
MESSAGES_RATIO = 0.60           # 消息可用空间

# 压缩触发与目标阈值
COMPRESSION_THRESHOLD_RATIO = 0.50   # 触发压缩的阈值（占 context window）
L1_TRIGGER_RATIO = 0.50              # L1 触发点
L2_TRIGGER_RATIO = 0.70              # L2 触发点
L3_TRIGGER_RATIO = 0.85              # L3 触发点
TARGET_AFTER_COMPRESSION_RATIO = 0.40  # 压缩目标（占 context window）
MAX_AFTER_COMPRESSION_RATIO = 0.75     # 压缩后硬上限（设计文档约束）


class ContextBudgetAllocator:
    """Token 预算分配器

    估算消息 token 数，计算各区域可用预算，判断是否需要压缩。
    """

    def __init__(self, context_window: int = DEFAULT_CONTEXT_WINDOW) -> None:
        self.context_window = max(context_window, 10_000)  # 最小 10k

    @property
    def system_prompt_reserve(self) -> int:
        """system prompt 预留 token"""
        return int(self.context_window * SYSTEM_PROMPT_RATIO)

    @property
    def tool_definitions_reserve(self) -> int:
        """工具定义预留 token"""
        return int(self.context_window * TOOL_DEFINITIONS_RATIO)

    @property
    def response_reserve(self) -> int:
        """模型回复预留 token"""
        return int(self.context_window * RESPONSE_RATIO)

    @property
    def available_for_messages(self) -> int:
        """消息可用 token 空间"""
        return int(self.context_window * MESSAGES_RATIO)

    @property
    def compression_threshold(self) -> int:
        """触发压缩的 token 阈值"""
        return int(self.context_window * COMPRESSION_THRESHOLD_RATIO)

    @property
    def l1_threshold(self) -> int:
        """L1 触发阈值"""
        return int(self.context_window * L1_TRIGGER_RATIO)

    @property
    def l2_threshold(self) -> int:
        """L2 触发阈值"""
        return int(self.context_window * L2_TRIGGER_RATIO)

    @property
    def l3_threshold(self) -> int:
        """L3 触发阈值"""
        return int(self.context_window * L3_TRIGGER_RATIO)

    @property
    def target_after_compression(self) -> int:
        """压缩目标 token 数"""
        return int(self.context_window * TARGET_AFTER_COMPRESSION_RATIO)

    @property
    def max_after_compression(self) -> int:
        """压缩后硬上限（设计文档：≤ 75%）"""
        return int(self.context_window * MAX_AFTER_COMPRESSION_RATIO)

    def estimate_tokens(self, messages: list[Any]) -> int:
        """估算消息列表的 token 数

        保守估算：每条消息有固定开销（role + metadata ≈ 4 tokens），
        内容按 3.5 chars/token 计算。
        """
        total_chars = 0
        for msg in messages:
            # 固定开销：role + metadata
            total_chars += 16  # ≈ 4 tokens
            # 内容
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # 多模态消息（图片等），按文本部分估算
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total_chars += len(item.get("text", ""))
                    elif isinstance(item, str):
                        total_chars += len(item)
            # tool_calls 开销
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                import json
                total_chars += len(json.dumps(tool_calls, default=str))
        return max(1, int(total_chars / CHARS_PER_TOKEN))

    def estimate_single_message_tokens(self, msg: Any) -> int:
        """估算单条消息的 token 数"""
        return self.estimate_tokens([msg])

    def needs_compression(self, current_tokens: int) -> bool:
        """判断是否需要压缩"""
        return current_tokens >= self.compression_threshold

    def get_budget_info(self) -> dict[str, Any]:
        """返回预算信息（用于日志/调试）"""
        return {
            "context_window": self.context_window,
            "system_prompt_reserve": self.system_prompt_reserve,
            "tool_definitions_reserve": self.tool_definitions_reserve,
            "response_reserve": self.response_reserve,
            "available_for_messages": self.available_for_messages,
            "compression_threshold": self.compression_threshold,
            "l1_threshold": self.l1_threshold,
            "l2_threshold": self.l2_threshold,
            "l3_threshold": self.l3_threshold,
            "target_after_compression": self.target_after_compression,
            "max_after_compression": self.max_after_compression,
        }
