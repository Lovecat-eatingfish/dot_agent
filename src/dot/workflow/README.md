# dot.workflow — 通用工作流引擎使用说明

自研的轻量工作流引擎：**节点 + 条件路由的图**，带重试/超时/补偿/取消/人工中断。
核心不依赖 Agent、模型 SDK 或任何编排框架（langgraph/langchain 均不需要）。

```
依赖方向（自上而下）：
  dot.coding  →  dot.workflow ─┐
  dot.agent   →  dot.ai      ─┼→ dot.core
  dot.workflow ────────────────┘   （workflow 只依赖 core）
```

> `dot.coding` 的 plan → code → validate 是本引擎的一个业务实例
> （AgentNode 适配器在 `dot.agent.workflow`），不是引擎本身。

---

## 快速上手

```python
import asyncio
from dot.workflow import END, FunctionNode, WorkflowContext, WorkflowGraph


async def main():
  graph = WorkflowGraph(name="demo")

  graph.add_node(FunctionNode("fetch", lambda ctx: {"price": 100}))  # 结果写入 ctx.results["fetch"]
  graph.add_node(FunctionNode("bill", lambda ctx: ctx.get_result("fetch")["price"] * 2))
  graph.set_entry("fetch")
  graph.add_edge("fetch", "bill")
  graph.add_edge("bill", END)  # END 是保留的虚拟出口

  ctx = WorkflowContext()
  async for event in graph.run(ctx):
    print(event.type, getattr(event, "node", ""))

  print(ctx.status, ctx.results)  # completed {'fetch': {...}, 'bill': 200}


asyncio.run(main())
```

三步定式：**add_node 定义节点 → set_entry/add_edge/add_conditional_edges 连边 → run(ctx) 消费事件流**。

---

## 核心概念

### 1. WorkflowNode 协议（唯一扩展点）

```python
class WorkflowNode(Protocol):
    name: str
    def run(self, ctx: WorkflowContext) -> AsyncIterator[Any]: ...
```

结构化协议，**零继承**：任何带 `name` 属性和 `async` 生成器 `run(ctx)` 的对象都是合法节点。
节点 `yield` 出的任何对象会被引擎**原样透传**（引擎只在其前后包一层 NodeStart/NodeEnd 生命周期事件）。

内置节点：

| 节点 | 用途 |
| --- | --- |
| `FunctionNode(name, fn, store_result=True)` | 执行同步/异步函数，返回值写入 `ctx.results[name]` |
| `ParallelNode(name, branches)` | 多个纯函数分支并发执行（fan-out/fan-in），聚合为 `{分支名: 返回值}` |
| `SubgraphNode(name, graph)` | 把一张完整子图作为节点嵌入，实现分层组合 |
| `AgentNode(...)`（`dot.agent.workflow`） | 上层适配器：把一次 agent turn 包装成节点 |

### 2. WorkflowContext（节点间通信的唯一通道）

| 成员 | 说明 |
| --- | --- |
| `ctx.data: dict` | 任意业务状态（引擎不解释内容） |
| `ctx.results: dict` | 节点名 → 节点产物（`set_result` / `get_result`） |
| `ctx.signal` | 只读取消令牌，节点在长耗时步骤间检查 `is_cancelled()` |
| `ctx.completed_nodes` | 已完成节点名列表 |
| `ctx.status` | `pending / running / paused / completed / failed / cancelled` |
| `ctx.error / error_code / error_details` | 失败信息（`mark_error` / `mark_cancelled`） |
| `ctx.to_report()` | 运行报告（run_id、状态、已完成节点、结果、错误） |

### 3. WorkflowEvent（事件流）

`graph.run(ctx)` 产出 discriminated union（`type` 字段判别）：

| 事件 | 关键字段 | 语义 |
| --- | --- | --- |
| `WorkflowNodeStartEvent` | `node, step, run_id` | 即将执行某节点 |
| `WorkflowNodeEndEvent` | `node, ok, error, attempts, duration` | 某节点执行完毕 |
| `WorkflowErrorEvent` | `node, error, step, details` | 因异常/超限/取消终止 |
| `WorkflowDoneEvent` | `run_id, step, duration` | 正常走完 |
| `WorkflowInterruptEvent` | `interrupt_id, node, reason, payload` | 节点请求人工决定（可恢复） |

错误码常量见 `ErrorCode`（`VALIDATION_ERROR / TIMEOUT / NODE_ERROR / ROUTING_ERROR / MAX_STEPS_EXCEEDED / CANCELLED / INTERRUPT_TIMEOUT / UNKNOWN`）。

---

## 特性详解

### 条件路由

每个节点**最多一条出边**（静态校验强制）；需要分支时用路由函数：

```python
def router(ctx: WorkflowContext) -> str | None:
    if ctx.get_result("validate")["passed"]:
        return END                # 返回 END（或 None）结束
    return "fix"                  # 返回目标节点名

graph.add_conditional_edges("validate", router)
```

### 重试 / 超时 / 指数退避

`add_node` 直接指定策略，重试自动带指数退避 + 抖动：

```python
graph.add_node(
    FunctionNode("call_api", call_api),
    retries=3,              # 最多重试 3 次
    timeout=10.0,           # 单次尝试 10 秒超时
    backoff_base=1.0,       # 首次退避 1s，之后 2s、4s...
    backoff_max=30.0,
    backoff_jitter=0.1,     # ±10% 抖动
)
```

### 补偿（失败清理）

节点失败（重试耗尽）后自动执行补偿节点；补偿自身失败只告警、不掩盖原错误：

```python
graph.add_node(FunctionNode("order", create_order),
               compensate_with=FunctionCompensationNode(
                   "cancel_order", lambda ctx, err: cancel_order(ctx)))
# 或实现 CompensationNode 协议（async compensate(ctx, error)）
```

### 人工中断与恢复（interrupt / resume）

节点内 `await ctx.interrupt(reason, payload=...)` 暂停整个图；
调用方收到 `WorkflowInterruptEvent` 后，用 `ctx.resume(value)` 恢复。
推荐直接用包装好的交互运行器（自动完成 resume 配对）：

```python
from dot.workflow import run_with_interaction

async def handler(event: WorkflowInterruptEvent) -> bool:
    return confirm(f"{event.reason}?")       # console / TUI 各自实现

async for event in run_with_interaction(graph, ctx, handler):
    ...
```

### 取消

调用方持有可写令牌，节点侧只读：

```python
ctx.signal.cancel()          # 运行中任意时刻调用
# 当前节点被立即打断（WorkflowCancellationError），
# 图在下一个节点边界停止并产出 WorkflowErrorEvent(CANCELLED)
```

### 死循环防护与静态校验

- `WorkflowGraph(max_steps=100)`：步数上限，超出按 `MAX_STEPS_EXCEEDED` 终止；
- `run()` 前自动执行 `validate()`：入口必须设置、不可达节点、所有节点须可达 END、DFS 环检测。
  校验失败抛 `WorkflowValidationError`。

### 并行与子图

```python
from dot.workflow import ParallelBranch, ParallelNode, SubgraphNode

# 并行：纯函数分支并发跑，聚合为 dict；任一分支失败 → 取消其余 + 走节点错误处理
graph.add_node(ParallelNode("fetch_all", (
    ParallelBranch("weather", fetch_weather),   # 同步/异步均可
    ParallelBranch("news", fetch_news),
)))

# 子图：完整嵌套一张图（data 共享、取消传播、事件透传、results 双向合并）
inner = WorkflowGraph(name="inner")
...
graph.add_node(SubgraphNode("stage_one", inner))
```

子图语义：子图运行在独立 context 上（避免 running 状态冲突），
`data` 按引用共享、`results` 以父图已有结果为初值并在结束后合并回父图、
取消令牌与父图共用、失败抛 `WorkflowError` 交给父图的重试/补偿策略。

### 导出与报告

```python
graph.to_dict()      # 结构化 dict（可 JSON 序列化）
graph.to_mermaid()   # Mermaid flowchart 文本，贴到 Markdown 即可渲染
ctx.to_report()      # 运行报告：状态 / 已完成节点 / 结果 / 错误
```

---

## 错误处理模型

```
节点内异常 / 超时
   ↓ 按重试策略重试（指数退避）
   ↓ 重试耗尽 → 执行补偿节点
   ↓ 产出 WorkflowNodeEndEvent(ok=False)
   ↓ 产出 WorkflowErrorEvent → 图终止，ctx.status = "failed"
```

注意：**节点内异常不会从 `graph.run()` 抛出**，而是转为错误事件（流式语义）。
定义期错误（重复节点、未知边、校验失败）才以异常形式抛出。

---

## 与 dot.coding 的关系

`dot.coding.workflow.build_coding_workflow()` 用本引擎组装 plan → code → validate
业务图：三个 `AgentNode`（来自 `dot.agent.workflow`，把 harness 的 agent turn
包装成节点）+ validate 后的条件路由 + replan 预算 + 人工介入节点。
阅读该文件是"如何用本引擎编排真实业务"的最佳示例。

## 测试

```bash
uv run pytest tests/test_workflow_engine.py tests/test_workflow_extensions.py -v
```

- `test_workflow_engine.py`：引擎核心（校验/路由/重试/取消/中断/补偿/导出）
- `test_workflow_extensions.py`：ParallelNode / SubgraphNode / workflow_name
