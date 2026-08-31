"""
dot.coding.state — coding 工作流的业务状态

通用的图 / 上下文抽象在 dot.workflow；这里只放 plan→code→validate
这条业务工作流自己的状态对象（挂在 WorkflowContext.data 中传递），
替代旧的 WorkflowPhase 显式状态机——阶段流转现在由图的边和路由函数表达。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """验证结果"""
    passed: bool = False
    message: str = ""
    issues: list[str] = field(default_factory=list)


@dataclass
class CodingWorkflowState:
    """plan→code→validate 工作流的业务状态"""
    task: str
    plan: str | None = None
    replan_count: int = 0
    max_replan: int = 3
    validate_result: ValidationResult | None = None
    validation_history: list[ValidationResult] = field(default_factory=list)
    error: str | None = None
    human_intervention_count: int = 0

    def should_replan(self) -> bool:
        """是否还有重新规划的机会"""
        return self.replan_count < self.max_replan

    def mark_replan(self) -> None:
        """记录一次 replan，清空上一轮验证结果"""
        self.replan_count += 1
        self.validate_result = None

    def continue_after_human_intervention(self) -> None:
        """人工确认后开启一轮新的 replan 预算"""
        self.human_intervention_count += 1
        self.replan_count = 0
        self.validate_result = None
        self.error = None
