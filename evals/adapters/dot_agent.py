"""
evals.adapters.dot_agent — dot_agent 专用适配器

驱动方式：.venv/Scripts/agent.exe run -w <workspace> "<task>"
轨迹来源：workspace/.dot/sessions/*/session.jsonl（复用 evals.metrics 解析）
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

try:
    from evals.trajectory import collect_metrics
except ImportError:  # 以 evals/ 为 sys.path 根直接运行时
    from trajectory import collect_metrics

from .base import RunResult, Trajectory


class DotAgentAdapter:
    """本仓库 dot_agent 的适配器"""

    name = "dot_agent"

    def __init__(self, repo_root: Path, timeout_s: int = 480) -> None:
        self.repo_root = repo_root
        self.timeout_s = timeout_s

    def _spawn_env(self) -> dict:
        """把 .venv/Scripts 前插到 PATH：agent bash 里的 python/pytest 与评估环境一致"""
        env = dict(os.environ)
        env["PATH"] = str(self.repo_root / ".venv" / "Scripts") + os.pathsep + env.get("PATH", "")
        return env

    def run(self, task_text: str, workspace: Path, run_dir: Path) -> RunResult:
        agent_exe = self.repo_root / ".venv" / "Scripts" / "agent.exe"
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [str(agent_exe), "run", "-w", str(workspace), task_text],
                cwd=str(self.repo_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self.timeout_s, env=self._spawn_env(),
            )
        except subprocess.TimeoutExpired:
            return RunResult(exit_code=-1, wall_seconds=round(time.perf_counter() - t0, 1),
                             error=f"timeout after {self.timeout_s}s")
        except OSError as exc:
            return RunResult(exit_code=-1, wall_seconds=0.0, error=f"{type(exc).__name__}: {exc}")

        wall = round(time.perf_counter() - t0, 1)
        log_path = run_dir / "agent.log"
        log_path.write_text(
            f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
            encoding="utf-8", errors="replace")
        return RunResult(exit_code=proc.returncode, wall_seconds=wall, log_path=log_path)

    def collect_trajectory(self, workspace: Path, log_text: str = "") -> Trajectory | None:
        sessions = sorted(workspace.glob(".dot/sessions/*/session.jsonl"))
        if not sessions:
            return None
        m = collect_metrics(sessions[-1])
        # 重试/限流次数：从原始日志统计（429 / retry 关键字，失败率与稳定性观测）
        retries = sum(log_text.lower().count(k) for k in ("429", "too many requests", "retry"))
        return Trajectory(
            turns=m.get("turns"), llm_calls=m.get("llm_calls"),
            tool_calls=m.get("tool_calls"), tool_names=m.get("tool_names") or {},
            tokens=m.get("tokens"), retries=retries,
            final_text=m.get("has_final_text"),
            out_of_workspace_args=(m.get("security") or {}).get("out_of_workspace_args") or [],
        )
