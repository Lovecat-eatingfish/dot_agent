"""
dot.workflow.node — 节点抽象

WorkflowNode 是唯一扩展点：任何带 name + async generator run(ctx) 的对象
都是合法节点。引擎不关心节点内部是 LLM 调用、纯函数还是人工等待。

内置节点：
  - FunctionNode  普通工作流节点：执行任意同步/异步可调用对象

Agent/LLM 节点属于上层适配器，不放在核心引擎中，避免引入具体的
AgentHarness、OpenAI SDK 或其他模型框架。
"""
from __future__ import annotations

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
