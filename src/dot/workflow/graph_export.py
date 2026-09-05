# dot.workflow.graph_export — 图的序列化导出
#
# 只读图结构，与图定义/执行解耦：WorkflowGraph.to_dict() / to_mermaid() 委托到这里。
# 通过鸭子类型访问图内部（同包内协作，不引入运行时 import）。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .graph import WorkflowGraph


def graph_to_dict(graph: "WorkflowGraph") -> dict[str, Any]:
    """导出为 dict（可序列化）"""
    return {
        "name": graph.name,
        "max_steps": graph.max_steps,
        "entry": graph.entry,
        "nodes": {
            name: {
                "policy": {
                    "retries": p.retries,
                    "timeout": p.timeout,
                    "backoff_base": p.backoff_base,
                    "backoff_max": p.backoff_max,
                    "backoff_jitter": p.backoff_jitter,
                },
                "has_compensation": name in graph._compensations,
            }
            for name, p in graph._policies.items()
        },
        "edges": dict(graph._edges),
        "routers": list(graph._routers.keys()),
    }


def graph_to_mermaid(graph: "WorkflowGraph") -> str:
    """导出为 Mermaid flowchart 语法"""
    lines = [f"flowchart LR", f"    %% {graph.name}"]

    # 节点定义
    for name in graph._nodes:
        policy = graph._policies.get(name)
        label = name
        if policy and (policy.retries > 0 or policy.timeout):
            attrs = []
            if policy.retries > 0:
                attrs.append(f"r={policy.retries}")
            if policy.timeout:
                attrs.append(f"t={policy.timeout}s")
            label = f"{name}\\n[{', '.join(attrs)}]"
        lines.append(f"    {name}(({label}))")

    # 边定义
    for source, target in graph._edges.items():
        if target == "__end__":
            lines.append(f"    {source} --> END((END))")
        else:
            lines.append(f"    {source} --> {target}")

    # 条件边
    for source in graph._routers:
        lines.append(f"    {source} -.-> {source}_cond{{?}}")

    # 入口标记
    if graph.entry:
        lines.append(f"    start((START)) --> {graph.entry}")

    return "\n".join(lines)
