"""
TurnState — 单轮执行状态

从 Session 中提取的 per-turn 状态，graph 节点直接读写。
每轮执行前由 reset() 重置，跨 turn 不持久（持久化由 persistence 层从 Session 整体快照）。

字段职责：
  - task:              当前用户输入
  - task_plan:         plan_node 生成的任务计划 JSON
  - replan_count:      本轮已重规划次数（coding_agent 判定 plan 无效时 +1）
  - attempt_count:     本轮已重试次数（valid_node 失败时 +1）
  - validate_result:   校验节点的结论（passed / error_msg / checks）
  - need_human_intervene:  coding_agent 标记需要人工介入
  - resume_action:     人工介入后的用户选择（continue / stop；空字符串=无介入）
  - plan_invalid:      coding_agent 标记当前 plan 无效（路由回 plan_node）
  - awaiting_intervention:  本轮已进入人工介入等待状态
  - is_running:        本轮正在执行（并发守卫）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnState:
    """单轮执行状态（per-turn reset，graph 节点读写）"""

    task: str = ""
    task_plan: dict = field(default_factory=dict)
    replan_count: int = 0
    attempt_count: int = 0
    validate_result: dict = field(default_factory=dict)
    need_human_intervene: bool = False
    resume_action: str = ""
    plan_invalid: bool = False
    awaiting_intervention: bool = False
    is_running: bool = False

    def reset(self) -> None:
        """每轮执行前重置（messages / 运行时状态保留）"""
        self.task = ""
        self.task_plan = {}
        self.replan_count = 0
        self.attempt_count = 0
        self.validate_result = {}
        self.need_human_intervene = False
        self.resume_action = ""
        self.plan_invalid = False
        self.awaiting_intervention = False
        self.is_running = False
