"""
evals.adapters — 被测 agent 适配器集

按名字解析适配器：
  dot_agent           → DotAgentAdapter（专用，含轨迹解析）
  fake                → FakeAdapter（测试假件，不调 LLM）
  其他任意名字         → evals/agents/<name>.yaml → GenericCLIAdapter（命令模板接入）
"""
from __future__ import annotations

from pathlib import Path

from .base import AgentAdapter, RunResult, Trajectory
from .cli_generic import GenericCLIAdapter
from .dot_agent import DotAgentAdapter
from .fake import FakeAdapter

__all__ = ["AgentAdapter", "RunResult", "Trajectory",
           "DotAgentAdapter", "GenericCLIAdapter", "FakeAdapter", "load_adapters"]


def load_adapters(spec: str, repo_root: Path, timeout_s: int = 480) -> list:
    """把 "a,b,c" 形式的名字列表解析成适配器实例列表"""
    import yaml

    agents_dir = repo_root / "evals" / "agents"
    adapters = []
    for name in [s.strip() for s in spec.split(",") if s.strip()]:
        if name == "dot_agent":
            adapters.append(DotAgentAdapter(repo_root=repo_root, timeout_s=timeout_s))
        elif name == "fake":
            adapters.append(FakeAdapter())
        else:
            config = agents_dir / f"{name}.yaml"
            if not config.is_file():
                raise FileNotFoundError(
                    f"未知 agent '{name}'：没有专用适配器，且找不到 {config}")
            adapters.append(GenericCLIAdapter.from_yaml(config))
    if not adapters:
        raise ValueError("未指定任何被测 agent")
    return adapters
