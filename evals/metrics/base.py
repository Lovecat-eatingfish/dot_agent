"""
evals.metrics.base — 指标框架核心契约（参考 DeepEval BaseMetric 的被验证模式）

分层思想（System One 式，确定性优先）：
  deterministic 层  零 LLM、零成本、可复现—— outcome / sandbox / efficiency / tool_usage
  llm 层            LLM judge—— 五维 rubric 评分，只处理确定性层无法判断的语义问题

所有指标实现同一协议，产出同一结果契约；runner 负责按 kind 与开关调度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# 指标种类
DETERMINISTIC = "deterministic"
LLM = "llm"


@dataclass
class EvalContext:
    """一次 (agent × task × run) 评测的全部现场——指标唯一的输入来源"""
    agent: str
    task: str
    category: str
    task_text: str
    task_meta: dict                     # task.yaml 内容（含 judge_rubric 等自定义项）
    workspace: Path                     # 沙箱
    run_dir: Path                       # 沙箱外目录（agent.log / 诱饵文件所在）
    agent_exit: int
    verified: bool                      # verify.py 硬判分结果（任务级 ground truth）
    verify_log: str
    trajectory: Any | None = None       # Trajectory | None（无遥测的 agent 为 None）
    log_text: str = ""                  # agent 原始输出（归因/judge 输入）
    leaked_to_root: list = field(default_factory=list)  # 仓库根逃逸探针结果（runner 提供）
    judge_samples: int = 1              # judge 多采样次数（降方差）


@dataclass
class MetricResult:
    """指标结果契约（对标 DeepEval BaseMetric 字段）"""
    name: str
    kind: str                           # DETERMINISTIC | LLM
    score: float | None = None          # 0-1 归一化（None = 该指标不适用）
    success: bool | None = None         # None = 不适用
    breakdown: dict = field(default_factory=dict)   # 明细（五维/子检查项）
    reason: str = ""
    verbose_logs: str = ""              # 打分过程留痕（judge 可审计性）
    cost: dict = field(default_factory=dict)        # llm_calls / judge tokens

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind,
            "score": self.score, "success": self.success,
            "breakdown": self.breakdown, "reason": self.reason,
            "verbose_logs": self.verbose_logs, "cost": self.cost,
        }


@runtime_checkable
class Metric(Protocol):
    """评测指标协议：确定性或 LLM，实现同一契约（零继承）"""

    name: str
    kind: str

    def run(self, ctx: EvalContext) -> MetricResult:
        ...
