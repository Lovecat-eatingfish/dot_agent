# dot.workflow.subgraph — SubgraphNode（子图节点）
#
# 把一张完整的 WorkflowGraph 作为单个节点嵌入更大的图，
# 实现分层的 workflow 组合：父图编排阶段，子图编排阶段内部细节。
#
# 语义约定：
#   - 子图运行在独立的 WorkflowContext 上（避免与父图的 running 状态冲突），
#     data 浅拷贝自父 ctx（业务对象按引用共享）；results 以父图的已有结果
#     为初值（子图可读父图产物），结束后合并回父 ctx
#   - 取消令牌直接复用父 ctx 的：父图取消 → 子图立即取消
#   - 子图事件原样透传（run_id 与父图不同，可用于区分来源）
#   - 子图结束后 results 合并回父 ctx；失败抛 WorkflowError 交给父图的
#     重试/补偿策略处理
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .context import WorkflowContext
from .graph import WorkflowCancellationError, WorkflowError, WorkflowGraph


class SubgraphNode:
    """子图节点：name + graph 即满足 WorkflowNode 协议"""

    def __init__(self, name: str, graph: WorkflowGraph) -> None:
        self.name = name
        self.graph = graph
        # 提前校验子图自身定义合法，让错误在定义期暴露
        graph.validate()

    async def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]:
        child = WorkflowContext()
        child.signal = ctx.signal          # 取消传播：父取消 → 子取消
        child.data.update(ctx.data)        # 业务状态按引用共享
        child.results.update(ctx.results)  # 子图可读父图已有产物

        try:
            async for event in self.graph.run(child):
                yield event
        finally:
            ctx.results.update(child.results)

        if child.signal.is_cancelled():
            raise WorkflowCancellationError
        if child.status == "failed":
            raise WorkflowError(
                child.error or "subgraph failed",
                node=self.name,
                code=child.error_code,
                details=child.error_details,
            )


__all__ = ["SubgraphNode"]
