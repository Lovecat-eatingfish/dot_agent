"""
dot.agent.compaction — 上下文压缩契约（agent 层定义，coding 层实现）

agent 循环在每轮工具执行完、下一轮 LLM 调用前，把消息历史交给
CompactionGate 检查；实现方（coding 层的 AutoCompactor）自行估算
占用、决定是否压缩，超阈值时返回压缩后的消息列表。

与 PermissionGate 同一模式：agent 只依赖本契约，不知道具体实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from dot.ai.types import AgentMessage


@dataclass
class CompactionResult:
    """一次自动压缩的结果"""
    messages: list["AgentMessage"]
    level: str = ""    # 已应用的级别，如 "L1+L2"、"L1+L2+L3"
    reason: str = ""   # 触发原因（给 UI 展示）


class CompactionGate(Protocol):
    """上下文压缩契约（coding 层的 AutoCompactor 满足该协议，无需显式继承）"""

    async def maybe_compact(self, messages: list["AgentMessage"]) -> "CompactionResult | None":
        """检查上下文占用，需要时压缩

        返回 None 表示无需压缩；返回 CompactionResult 时
        其 messages 为压缩后的完整消息历史（由调用方原地替换）。
        """
        ...
