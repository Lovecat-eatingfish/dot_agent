"""
dot.agent.loop_detector — 工具调用循环检测器

检测 Agent Loop 中的工具调用死循环：
- 连续相同工具调用
- 相同参数重复调用
- 短窗口内高频调用
- ABAB 工具对循环

当检测到循环时，返回 ToolLoopViolation 信息，由调用方决定处理策略（注入提示/终止等）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

__all__ = [
    "ToolCallRecord",
    "ToolCallTracker",
    "ToolLoopViolation",
]


# ============================================================
# 数据结构
# ============================================================


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """工具调用记录"""
    turn: int
    tool_name: str
    arguments_hash: str
    timestamp: float


@dataclass
class ToolLoopViolation:
    """循环违规信息"""
    violation_type: Literal["consecutive", "same_args", "window_freq", "pair_cycle"]
    tool_name: str
    description: str
    count: int
    window: int | None = None
    same_args_count: int = 0
    suggestion: str = ""


# ============================================================
# 配置预设
# ============================================================

DEFAULT_CONFIG = {
    "max_consecutive_same_tool": 3,
    "max_same_args_repeat": 2,
    "window_size": 5,
    "max_same_tool_in_window": 3,
    "max_pair_cycle": 2,
}

STRICT_CONFIG = {
    "max_consecutive_same_tool": 2,
    "max_same_args_repeat": 1,
    "window_size": 3,
    "max_same_tool_in_window": 2,
    "max_pair_cycle": 1,
}

LENIENT_CONFIG = {
    "max_consecutive_same_tool": 5,
    "max_same_args_repeat": 3,
    "window_size": 10,
    "max_same_tool_in_window": 5,
    "max_pair_cycle": 3,
}


# ============================================================
# 工具函数
# ============================================================


def _hash_arguments(arguments: dict) -> str:
    """计算参数的 hash（用于快速比较）"""
    try:
        # 排序确保相同参数产生相同 hash
        serialized = json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.md5(serialized.encode()).hexdigest()[:12]
    except Exception:
        return str(id(arguments))


# ============================================================
# ToolCallTracker
# ============================================================


class ToolCallTracker:
    """工具调用追踪器

    使用滑动窗口 + 模式匹配检测工具调用循环。
    """

    def __init__(
        self,
        max_consecutive_same_tool: int = 3,
        max_same_args_repeat: int = 2,
        window_size: int = 5,
        max_same_tool_in_window: int = 3,
        max_pair_cycle: int = 2,
    ) -> None:
        if max_consecutive_same_tool < 1:
            raise ValueError("max_consecutive_same_tool must be >= 1")
        if max_same_args_repeat < 1:
            raise ValueError("max_same_args_repeat must be >= 1")
        if window_size < 2:
            raise ValueError("window_size must be >= 2")
        if max_same_tool_in_window < 1:
            raise ValueError("max_same_tool_in_window must be >= 1")
        if max_pair_cycle < 1:
            raise ValueError("max_pair_cycle must be >= 1")

        self._max_consecutive = max_consecutive_same_tool
        self._max_same_args = max_same_args_repeat
        self._window_size = window_size
        self._max_in_window = max_same_tool_in_window
        self._max_pair_cycle = max_pair_cycle

        self._history: deque[ToolCallRecord] = deque(maxlen=window_size * 2)
        self._consecutive_tool: str | None = None
        self._consecutive_count: int = 0

    @classmethod
    def from_preset(cls, preset: Literal["default", "strict", "lenient"] = "default") -> ToolCallTracker:
        """从预设配置创建追踪器"""
        configs = {
            "default": DEFAULT_CONFIG,
            "strict": STRICT_CONFIG,
            "lenient": LENIENT_CONFIG,
        }
        return cls(**configs[preset])

    def record(self, tool_name: str, arguments: dict, turn: int = 0, timestamp: float = 0.0) -> None:
        """记录一次工具调用

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            turn: 当前轮次
            timestamp: 时间戳
        """
        record = ToolCallRecord(
            turn=turn,
            tool_name=tool_name,
            arguments_hash=_hash_arguments(arguments),
            timestamp=timestamp,
        )
        self._history.append(record)

        # 更新连续计数
        if tool_name == self._consecutive_tool:
            self._consecutive_count += 1
        else:
            self._consecutive_tool = tool_name
            self._consecutive_count = 1

    def check_violation(self) -> ToolLoopViolation | None:
        """检查是否触发循环检测

        Returns:
            ToolLoopViolation 如果检测到循环，否则 None
        """
        if len(self._history) < 2:
            return None

        # 检测1：连续相同工具
        violation = self._check_consecutive()
        if violation:
            return violation

        # 检测2：相同参数重复
        violation = self._check_same_args()
        if violation:
            return violation

        # 检测3：窗口频率
        violation = self._check_window_frequency()
        if violation:
            return violation

        # 检测4：ABAB 循环
        violation = self._check_pair_cycle()
        if violation:
            return violation

        return None

    def reset(self) -> None:
        """重置追踪器"""
        self._history.clear()
        self._consecutive_tool = None
        self._consecutive_count = 0

    @property
    def history(self) -> list[ToolCallRecord]:
        """获取调用历史"""
        return list(self._history)

    # ============================================================
    # 内部检测方法
    # ============================================================

    def _check_consecutive(self) -> ToolLoopViolation | None:
        """检测连续相同工具调用"""
        if self._consecutive_count >= self._max_consecutive:
            return ToolLoopViolation(
                violation_type="consecutive",
                tool_name=self._consecutive_tool or "",
                description=f"工具 {self._consecutive_tool} 连续调用 {self._consecutive_count} 次",
                count=self._consecutive_count,
                suggestion="尝试使用不同的工具或方法，避免重复相同操作",
            )
        return None

    def _check_same_args(self) -> ToolLoopViolation | None:
        """检测相同参数重复调用"""
        if len(self._history) < 2:
            return None

        last = self._history[-1]
        same_args_count = sum(
            1 for r in self._history
            if r.tool_name == last.tool_name and r.arguments_hash == last.arguments_hash
        )

        if same_args_count >= self._max_same_args:
            return ToolLoopViolation(
                violation_type="same_args",
                tool_name=last.tool_name,
                description=f"工具 {last.tool_name} 使用相同参数调用 {same_args_count} 次",
                count=same_args_count,
                same_args_count=same_args_count,
                suggestion="相同参数的重复调用通常表示操作失败，请尝试不同的参数或方法",
            )
        return None

    def _check_window_frequency(self) -> ToolLoopViolation | None:
        """检测窗口内高频调用"""
        if len(self._history) < self._window_size:
            return None

        # 取最近 window_size 条记录
        window = list(self._history)[-self._window_size:]

        # 统计每个工具的调用次数
        tool_counts: dict[str, int] = {}
        for record in window:
            tool_counts[record.tool_name] = tool_counts.get(record.tool_name, 0) + 1

        # 检查是否有工具超过阈值
        for tool_name, count in tool_counts.items():
            if count >= self._max_in_window:
                return ToolLoopViolation(
                    violation_type="window_freq",
                    tool_name=tool_name,
                    description=f"工具 {tool_name} 在最近 {self._window_size} 轮中调用 {count} 次",
                    count=count,
                    window=self._window_size,
                    suggestion=f"在 {self._window_size} 轮内频繁调用同一工具可能表示陷入循环",
                )
        return None

    def _check_pair_cycle(self) -> ToolLoopViolation | None:
        """检测 ABAB 工具对循环

        检测连续的 ABABAB... 模式。
        每2条记录 [A, B] 算一个周期，连续重复算循环。
        """
        if len(self._history) < 4:
            return None

        recent = list(self._history)

        # 从后往前检测连续的 AB 对
        # 首先确定最后的 AB 模式
        a_tool = recent[-2].tool_name
        b_tool = recent[-1].tool_name

        # 检查是否是 AB 模式（最后两个工具不同）
        if a_tool == b_tool:
            return None

        # 统计连续的 AB 对数量
        cycle_count = 0
        i = len(recent) - 1

        while i >= 1:
            if recent[i].tool_name == b_tool and recent[i - 1].tool_name == a_tool:
                cycle_count += 1
                i -= 2
            else:
                break

        if cycle_count >= self._max_pair_cycle:
            return ToolLoopViolation(
                violation_type="pair_cycle",
                tool_name=f"{a_tool} ↔ {b_tool}",
                description=f"检测到 {a_tool} 和 {b_tool} 的交替循环（{cycle_count} 次）",
                count=cycle_count,
                suggestion=f"工具 {a_tool} 和 {b_tool} 交替调用可能表示陷入循环，请尝试不同的方法",
            )

        return None
