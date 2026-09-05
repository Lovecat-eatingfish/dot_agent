"""
dot.workflow.node — 节点抽象

WorkflowNode 是唯一扩展点：任何带 name + async generator run(ctx) 的对象
都是合法节点。引擎不关心节点内部是 LLM 调用、纯函数还是人工等待。

内置节点：
  - FunctionNode  普通工作流节点：执行任意同步/异步可调用对象
  - ParallelNode  并行节点：多个纯函数分支 fan-out/fan-in，结果聚合为 dict

Agent/LLM 节点属于上层适配器，不放在核心引擎中，避免引入具体的
AgentHarness、OpenAI SDK 或其他模型框架。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .context import WorkflowContext

class WorkflowNode(Protocol):
    """workflow 节点协议（结构化，零继承）"""

    name: str

    def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]:
        """执行节点；yield 出的任何对象由引擎原样转发"""
        ...


@dataclass(frozen=True, slots=True)
class FunctionNode:
    """普通工作流节点：执行 fn(ctx)，返回值写入 ctx.results[name]"""
    name: str
    fn: Callable[[WorkflowContext], Any]
    store_result: bool = True

    async def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]:
        result = self.fn(ctx)
        if inspect.isawaitable(result):
            result = await result
        if self.store_result:
            ctx.set_result(self.name, result)
        if False:  # 使本函数成为 async generator，但不产出任何事件
            yield None


@dataclass(frozen=True, slots=True)
class ParallelBranch:
    """ParallelNode 的一个分支：名字 + 纯函数"""
    name: str
    fn: Callable[[WorkflowContext], Any]


@dataclass(frozen=True, slots=True)
class ParallelNode:
    """并行节点：所有分支并发执行（fan-out/fan-in）

    分支应为纯函数（不产事件、不做人工等待）；任一分支抛异常时
    取消其余分支并把异常交给引擎（按节点策略重试/补偿）。
    聚合结果 {分支名: 返回值} 写入 ctx.results[name]。
    """
    name: str
    branches: tuple[ParallelBranch, ...]
    store_result: bool = True

    async def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]:
        if not self.branches:
            raise ValueError(f"parallel node {self.name} has no branches")
        branch_names = [b.name for b in self.branches]
        if len(set(branch_names)) != len(branch_names):
            raise ValueError(f"parallel node {self.name} has duplicate branch names")

        async def run_branch(branch: ParallelBranch) -> tuple[str, Any]:
            result = branch.fn(ctx)
            if inspect.isawaitable(result):
                result = await result
            return branch.name, result

        tasks = [asyncio.ensure_future(run_branch(b)) for b in self.branches]
        try:
            pairs = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        if self.store_result:
            ctx.set_result(self.name, dict(pairs))
        if False:  # 使本函数成为 async generator，但不产出任何事件
            yield None
