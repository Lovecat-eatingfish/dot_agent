"""
evals.adapters.cli_generic — 通用 CLI 适配器：一份 YAML 接入任意命令行 agent

配置文件放 evals/agents/<name>.yaml，最小示例：

    name: claude-code
    argv: ["claude", "-p", "{task}"]        # 模板变量：{task} {workspace}
    # workspace 传入方式示例：argv: ["claude", "--cwd", "{workspace}", "-p", "{task}"]
    env: {}                                  # 可选，附加环境变量
    timeout_s: 480

说明：
- 优先用 argv 数组（避免引号/空格转义问题）；也接受 command 字符串（按空格粗切分）
- trajectory 固定为 none（无遥测，评估降级为只看结果）；有日志格式的 agent
  请写专用 Adapter
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .base import RunResult


def _substitute(argv: list[str], task_text: str, workspace: Path) -> list[str]:
    return [a.replace("{task}", task_text).replace("{workspace}", str(workspace)) for a in argv]


class GenericCLIAdapter:
    """按命令模板驱动的通用 CLI agent 适配器"""

    def __init__(self, name: str, argv: list[str], env: dict[str, str] | None = None,
                 timeout_s: int = 480, cwd: Path | None = None) -> None:
        self.name = name
        self.argv = argv
        self.extra_env = env or {}
        self.timeout_s = timeout_s
        self.cwd = cwd  # None = 继承 runner 进程 CWD

    @classmethod
    def from_yaml(cls, path: Path) -> "GenericCLIAdapter":
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if data.get("argv"):
            argv = [str(a) for a in data["argv"]]
        elif data.get("command"):
            argv = str(data["command"]).split()
        else:
            raise ValueError(f"{path.name}: 需要 argv 或 command 字段")
        return cls(
            name=str(data.get("name") or path.stem),
            argv=argv,
            env=data.get("env") or None,
            timeout_s=int(data.get("timeout_s", 480)),
        )

    def run(self, task_text: str, workspace: Path, run_dir: Path) -> RunResult:
        import os

        env = dict(os.environ)
        env.update(self.extra_env)
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                _substitute(self.argv, task_text, workspace),
                cwd=str(self.cwd) if self.cwd else None,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout_s, env=env,
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

    def collect_trajectory(self, workspace: Path):
        return None  # 通用 CLI 无统一遥测，评估降级为只看结果
