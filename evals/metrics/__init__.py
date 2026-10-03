"""
evals.metrics — 指标注册表与默认管道

指标分两层（System One 式，确定性优先）：
  deterministic  Outcome / Sandbox / Efficiency / ToolUsage —— 零 LLM，永远执行
  llm            FiveDimJudge —— 五维 rubric 评分，仅在 --judge 且任务允许时执行

新增指标 = 实现 metrics.base.Metric 协议（一个 run(ctx) -> MetricResult），
注册进 DEFAULT_METRICS / JUDGE_METRICS 即被 runner 管道调度。
"""
from __future__ import annotations

from .base import DETERMINISTIC, LLM, EvalContext, Metric, MetricResult
from .deterministic import EfficiencyMetric, OutcomeMetric, SandboxMetric, ToolUsageMetric
from .llm import FiveDimJudge

__all__ = ["DETERMINISTIC", "LLM", "EvalContext", "Metric", "MetricResult",
           "OutcomeMetric", "SandboxMetric", "EfficiencyMetric", "ToolUsageMetric",
           "FiveDimJudge", "build_metric_pipeline"]

# 永远执行的确定性指标（顺序即报告呈现顺序）
DETERMINISTIC_METRICS: list = [OutcomeMetric, SandboxMetric, EfficiencyMetric, ToolUsageMetric]
# 需要 LLM 的指标（受 --judge 与 task.yaml judge 开关双重控制）
JUDGE_METRICS: list = [FiveDimJudge]


def build_metric_pipeline(with_judge: bool) -> list:
    """按开关组装本次运行的指标实例列表"""
    pipeline = [cls() for cls in DETERMINISTIC_METRICS]
    if with_judge:
        pipeline += [cls() for cls in JUDGE_METRICS]
    return pipeline
