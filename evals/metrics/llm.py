"""
evals.metrics.llm — LLM 指标层：五维 rubric judge（G-Eval 式）

确定性层无法判断的语义问题交给这一层：指令遵循/完整性/忠实度/有用性。
支持任务级自定义评审要点（task.yaml 的 judge_criteria）、多采样降方差、
judge 自身 token 成本核算与全过程留痕（verbose_logs，judge 可审计）。
"""
from __future__ import annotations

from judge import run_judge_multi

from .base import EvalContext, LLM, MetricResult


class FiveDimJudge:
    """五维 judge 指标：accuracy / instruction / completeness / faithfulness / helpfulness"""
    name = "five_dim_judge"
    kind = LLM
    THRESHOLD = 0.6  # 均分 3/5 以上视为通过（辅助信号，非硬门槛）

    def run(self, ctx: EvalContext) -> MetricResult:
        if not ctx.trajectory and not ctx.log_text:
            return MetricResult(name=self.name, kind=self.kind, score=None, success=None,
                                reason="无遥测且无日志，无法评审")

        criteria = ctx.task_meta.get("judge_criteria") or None
        j = run_judge_multi(ctx.task_text, ctx.workspace, agent_log=ctx.log_text[-3000:],
                            samples=ctx.judge_samples, criteria=criteria)
        dims = {d: j.get(d) for d in ("accuracy", "instruction", "completeness",
                                      "faithfulness", "helpfulness")}
        score = j.get("score")
        normalized = round(score / 5.0, 3) if score is not None else None
        return MetricResult(
            name=self.name, kind=self.kind,
            score=normalized,
            success=(None if normalized is None else normalized >= self.THRESHOLD),
            breakdown={**dims, "score_1_to_5": score, "samples": j["cost"].get("samples", 1)},
            reason=j.get("reason", ""),
            verbose_logs=j.get("verbose_logs", ""),
            cost=j.get("cost", {}),
        )
