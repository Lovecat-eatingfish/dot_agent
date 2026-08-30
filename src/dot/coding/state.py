"""
dot.coding.state — 外层 Workflow 状态

WorkflowPhase 枚举 + WorkflowContext dataclass，
替代 LangGraph 的隐式状态机（3 个布尔 flag）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WorkflowPhase(Enum):
    """外层 workflow 的显式状态机"""
    PLAN = "plan"
    CODE = "code"
    VALIDATE = "validate"
    HUMAN_INTERVENE = "human_intervene"
    DONE = "done"


@dataclass
class ValidationResult:
    """验证结果"""
    passed: bool = False
    message: str = ""
    issues: list[str] = field(default_factory=list)


@dataclass
class WorkflowContext:
    """外层 workflow 的状态容器

    通过函数返回值传递结果，不通过共享 Session 字段通信。
    """
    task: str = ""
    plan: str | None = None
    # 重新规划次数
    replan_count: int = 0
    validate_result: ValidationResult | None = None
    # work flow 的 状态机
    phase: WorkflowPhase = WorkflowPhase.PLAN
    # 最大重新规划次数
    max_replan: int = 3
    # 错误信息
    error: str | None = None

    def should_replan(self) -> bool:
        """是否应该重新规划"""
        return self.replan_count < self.max_replan

    def mark_replan(self) -> None:
        self.replan_count += 1
        self.phase = WorkflowPhase.PLAN
        self.validate_result = None

    def mark_done(self) -> None:
        self.phase = WorkflowPhase.DONE

    def mark_error(self, error: str) -> None:
        self.error = error
        self.phase = WorkflowPhase.DONE
