"""
evals.adapters.fake — 测试假件：脚本化"agent"，不调用 LLM

用途：
1. 框架自测：单元测试里跑全链路（runner → verify → 指标 → 报告）而不烧 token
2. 流水线冒烟：接入真实任务集验证判分器本身是否配置正确

行为由 behaviors 字典驱动，key 为任务名前缀：

    FakeAdapter(behaviors={
        "001": {"files": {"hello.py": "print('hello, eval!')"}, "exit_code": 0},
        "008": {"files_outside_ws": {"stray.txt": "x"}},   # 模拟越界写入（写进 run_dir）
    })

未命中任何前缀的任务按 default_exit 处理（不写文件）。
"""
from __future__ import annotations

import time
from pathlib import Path

from .base import RunResult, Trajectory


class FakeAdapter:
    name = "fake"

    def __init__(self, behaviors: dict[str, dict] | None = None, default_exit: int = 0,
                 tool_calls: int = 1) -> None:
        self.behaviors = behaviors or {}
        self.default_exit = default_exit
        self.tool_calls = tool_calls
        self._last_activity = False  # 最近一次 run 是否执行了行为（模拟真实活动）

    def run(self, task_text: str, workspace: Path, run_dir: Path) -> RunResult:
        # run_one_task 会把任务名编码进 run_dir（<stamp>-<agent>-<task>），用后缀匹配
        behavior = {}
        for prefix, spec in self.behaviors.items():
            if run_dir.name.endswith(prefix) or prefix in run_dir.name:
                behavior = spec
                break

        for rel, content in (behavior.get("files") or {}).items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel, content in (behavior.get("files_outside_ws") or {}).items():
            target = run_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        time.sleep(0.01)  # 模拟最小耗时
        self._last_activity = bool(behavior)
        return RunResult(exit_code=int(behavior.get("exit_code", self.default_exit)),
                         wall_seconds=0.01)

    def collect_trajectory(self, workspace: Path) -> Trajectory:
        if not self._last_activity:
            # 未执行任何行为 → 零活动轨迹（供 infra_error 识别测试）
            return Trajectory(turns=1, llm_calls=0, tool_calls=0, tokens=None)
        return Trajectory(turns=1, llm_calls=1, tool_calls=self.tool_calls,
                          tool_names={"fake_tool": self.tool_calls}, tokens=None)
