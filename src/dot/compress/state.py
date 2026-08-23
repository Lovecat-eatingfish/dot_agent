"""
压缩状态管理 — 记录压缩历史与统计

设计约束（对齐设计文档）：
  - 压缩历史最多保留 20 条
  - 每次压缩记录 level / trigger / before-after 消息数与 token 数
  - 跨 turn 持久（不随 reset_per_turn 清除）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.log import get_logger
from ..core.utils import utc_now

logger = get_logger(__name__)

# 压缩历史最大保留条数
MAX_HISTORY_ENTRIES = 20


@dataclass
class CompressionHistoryEntry:
    """单次压缩记录"""
    timestamp: str = ""
    level: str = ""             # "L1" | "L2" | "L3"
    trigger: str = ""           # "auto" | "manual"
    before_messages: int = 0
    after_messages: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    summary: str = ""           # 压缩摘要（L2/L3 生成的摘要文本）


@dataclass
class CompressionState:
    """压缩状态（挂在 Session 上，跨 turn 持久）

    字段：
      history: 压缩历史（最多 MAX_HISTORY_ENTRIES 条）
      l2_last_trigger_turn: L2 上次触发的 turn（-1 表示从未触发）
      l3_last_trigger_turn: L3 上次触发的 turn（-1 表示从未触发）
      total_compressions: 累计压缩次数
      total_tokens_saved: 累计节省的 token 数
    """
    history: list[CompressionHistoryEntry] = field(default_factory=list)
    l2_last_trigger_turn: int = -1
    l3_last_trigger_turn: int = -1
    total_compressions: int = 0
    total_tokens_saved: int = 0

    def record(
        self,
        level: str,
        trigger: str,
        before_messages: int,
        after_messages: int,
        before_tokens: int,
        after_tokens: int,
        summary: str = "",
    ) -> None:
        """记录一次压缩事件"""
        entry = CompressionHistoryEntry(
            timestamp=utc_now(),
            level=level,
            trigger=trigger,
            before_messages=before_messages,
            after_messages=after_messages,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            summary=summary[:500],
        )
        self.history.append(entry)
        # 超限裁剪（保留最新的）
        if len(self.history) > MAX_HISTORY_ENTRIES:
            self.history = self.history[-MAX_HISTORY_ENTRIES:]
        self.total_compressions += 1
        saved = max(0, before_tokens - after_tokens)
        self.total_tokens_saved += saved
        logger.info(
            "[CompressionState] recorded %s (%s): %d msgs → %d msgs, %d → %d tokens (saved %d)",
            level, trigger, before_messages, after_messages, before_tokens, after_tokens, saved,
        )

    def update_trigger_turn(self, level: str, turn_id: int) -> None:
        """更新指定级别的上次触发 turn"""
        if level == "L2":
            self.l2_last_trigger_turn = turn_id
        elif level == "L3":
            self.l3_last_trigger_turn = turn_id

    def turns_since_l2(self, current_turn: int) -> int:
        """距上次 L2 触发的轮数（从未触发返回 999）"""
        if self.l2_last_trigger_turn < 0:
            return 999
        return current_turn - self.l2_last_trigger_turn

    def turns_since_l3(self, current_turn: int) -> int:
        """距上次 L3 触发的轮数（从未触发返回 999）"""
        if self.l3_last_trigger_turn < 0:
            return 999
        return current_turn - self.l3_last_trigger_turn

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 安全 dict"""
        return {
            "history": [
                {
                    "timestamp": e.timestamp,
                    "level": e.level,
                    "trigger": e.trigger,
                    "before_messages": e.before_messages,
                    "after_messages": e.after_messages,
                    "before_tokens": e.before_tokens,
                    "after_tokens": e.after_tokens,
                    "summary": e.summary,
                }
                for e in self.history
            ],
            "l2_last_trigger_turn": self.l2_last_trigger_turn,
            "l3_last_trigger_turn": self.l3_last_trigger_turn,
            "total_compressions": self.total_compressions,
            "total_tokens_saved": self.total_tokens_saved,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompressionState:
        """从 dict 反序列化"""
        if not isinstance(data, dict):
            return cls()
        history = []
        for entry in data.get("history", []):
            if isinstance(entry, dict):
                history.append(CompressionHistoryEntry(
                    timestamp=entry.get("timestamp", ""),
                    level=entry.get("level", ""),
                    trigger=entry.get("trigger", ""),
                    before_messages=int(entry.get("before_messages", 0)),
                    after_messages=int(entry.get("after_messages", 0)),
                    before_tokens=int(entry.get("before_tokens", 0)),
                    after_tokens=int(entry.get("after_tokens", 0)),
                    summary=entry.get("summary", ""),
                ))
        return cls(
            history=history,
            l2_last_trigger_turn=int(data.get("l2_last_trigger_turn", -1)),
            l3_last_trigger_turn=int(data.get("l3_last_trigger_turn", -1)),
            total_compressions=int(data.get("total_compressions", 0)),
            total_tokens_saved=int(data.get("total_tokens_saved", 0)),
        )
