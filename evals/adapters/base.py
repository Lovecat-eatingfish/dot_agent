"""
evals.adapters.base — 被测 agent 的统一抽象（评估平台的扩展点）

任何 agent 接入评估只需满足 AgentAdapter 协议：
  run()                执行一个任务（同步；子进程 / HTTP 均可）
  collect_trajectory() 把自家日志翻译成归一化 Trajectory（拿不到轨迹返回 None，
                       评估自动降级为只看结果）

零继承 Protocol：适配器不继承任何基类，只满足类型约定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class RunResult:
    """一次任务执行的原始结果"""
    exit_code: int
    wall_seconds: float
    log_path: Path | None = None   # 原始 stdout/stderr 落盘位置（失败归因用）
    error: str | None = None       # 适配器自身故障（如进程启动失败）


@dataclass
class Trajectory:
    """归一化轨迹：各家日志格式不同，由各自适配器翻译成这个形状

    字段允许为 None（该 agent 不提供此类遥测），统计层按缺失降级。
    """
    turns: int | None = None
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_names: dict[str, int] = field(default_factory=dict)
    tokens: int | None = None
    retries: int | None = None          # provider 限流/重试次数（失败率与稳定性观测）
    final_text: bool | None = None      # 是否产生了最终可见回答（限流降级识别）
    out_of_workspace_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "turns": self.turns,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "tool_names": dict(self.tool_names),
            "tokens": self.tokens,
            "retries": self.retries,
            "has_final_text": self.final_text,
            "security": {"out_of_workspace_args": list(self.out_of_workspace_args)},
        }


@runtime_checkable
class AgentAdapter(Protocol):
    """被测 agent 协议：评估平台只依赖这三个成员"""

    name: str

    def run(self, task_text: str, workspace: Path, run_dir: Path) -> RunResult:
        """在 workspace 内执行一个任务；run_dir 用于落原始日志等适配器私有产物"""
        ...
    def collect_trajectory(self, workspace: Path) -> Trajectory | None:
        """从 agent 自身的日志/产物提取归一化轨迹；无遥测能力返回 None"""
        ...
