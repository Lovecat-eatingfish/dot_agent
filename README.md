# dot_agent v2.0

自研工作流引擎与 Coding Agent 系统，工作流编排不依赖 langgraph/langchain。
LLM/Agent 调用通过适配器接入，具体模型实现可以独立选择。

## 架构概览

```text
┌─────────────────────────────────────────────────────┐
│  dot.coding  — Coding 应用层                        │
│  CLI / Session / Extensions / MCP / Skills          │
│  依赖：dot.workflow + typer + rich + mcp            │
├─────────────────────────────────────────────────────┤
│  dot.workflow — 通用工作流引擎                      │
│  WorkflowGraph / Node / 事件 / 取消                 │
│  核心不依赖 Agent、模型 SDK 或编排框架              │
├─────────────────────────────────────────────────────┤
│  dot.agent   — Agent 核心层                         │
│  Agent Loop / Tools / Events / Harness / History    │
│  依赖：dot.ai + pydantic                            │
├─────────────────────────────────────────────────────┤
│  dot.ai      — Provider 抽象层                      │
│  ModelProvider / Events / Catalog / Limits          │
│  依赖：仅 httpx                                     │
├─────────────────────────────────────────────────────┤
│  dot.core    — 共享内核（最底层）                    │
│  WireModel 序列化基类 / 取消令牌 Protocol           │
│  不依赖任何 dot.* 模块，被所有层依赖                │
└─────────────────────────────────────────────────────┘
```

层间依赖保持单向：`coding → workflow`，`coding → agent → ai → core`，
`workflow → core`；`workflow` 核心不依赖 Agent、模型 SDK 或编排框架。
权限契约（Decision / PermissionGate Protocol）由 `dot.agent.permission` 定义、
`dot.coding.permission` 实现，避免 agent → coding 反向依赖。

## 核心设计

### 双循环架构

```text
Outer Loop (WorkflowGraph)      Inner Loop (Agent Loop)
plan → code → validate          LLM call → tool exec → append
     ↑              |                (steering / follow-up)
     └──────────────┘
```

- **外层**：通用工作流引擎 `dot.workflow`（节点 + 条件路由的图），
  coding 的 plan→code→validate 是它的一个业务实例，不是引擎本身
- **内层**：`run_agent_loop` 流式响应 → 工具执行 → 循环

引擎内核（`src/dot/workflow/`），**完整使用说明见 [src/dot/workflow/README.md](src/dot/workflow/README.md)**：

| 模块 | 职责 |
| --- | --- |
| `graph.py` | `WorkflowGraph`：add_node / add_edge / add_conditional_edges（定义 + 校验/执行/导出委托到下列模块） |
| `graph_runner.py` | GraphRunner：执行循环（重试/退避/补偿/取消，max_steps 防死循环） |
| `graph_validate.py` | GraphValidator：静态校验（入口/不可达/END 可达性/环检测） |
| `graph_export.py` | to_dict / to_mermaid 序列化导出 |
| `node.py` | `WorkflowNode` Protocol + `FunctionNode` / `ParallelNode`；Agent 节点由上层适配器提供 |
| `subgraph.py` | `SubgraphNode`：图嵌套子图（data 共享、取消传播、事件透传） |
| `interaction.py` | `run_with_interaction`：中断事件 ↔ 人工处理器的通用桥接 |
| `context.py` | `WorkflowContext`：节点间数据（data/results）+ 取消令牌 + 中断/恢复 |
| `events.py` | `WorkflowEvent` discriminated union（NodeStart/NodeEnd/Error/Done/Interrupt） |

### 工具系统

```python
@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    parameters: dict          # JSON Schema
    execute_fn: ToolExecutor  # async callable
```

零继承，`frozen=True` 防篡改。内置工具：`read_file` / `write_file` / `edit_file` / `bash` / `glob_search` / `grep`。

### 事件系统

三层事件，Pydantic discriminated union：

| 层 | 事件 | 用途 |
| --- | --- | --- |
| `ProviderEvent` | TextDelta / ToolCallStart/End / Done | LLM 流式响应 |
| `AgentEvent` | TurnStart/End / MessageStart/End / ToolExec... | Agent 生命周期 |
| `CodingEvent` | Compaction / SessionInfoChanged | 编码会话 |

### 权限管控

三级拦截（顺序不可改）：

1. **系统黑名单**：危险命令正则、路径遍历防护
2. **项目黑名单**：`.agent-security.json`
3. **模式规则**：plan/edit/auto

决策三态：ALLOW / ASK / DENY。ASK 无 UI 时自动降级 DENY。

模式规则（文件写 = write_file / edit_file）：

| 模式 | 文件写 | bash | 说明 |
| --- | --- | --- | --- |
| plan | ASK（带 diff 预览） | DENY | 纯只读规划 |
| edit | ASK（带 diff 预览） | ASK | 对标 Claude Code 编辑审批；审批框 `a` 键可会话级免确认 |
| auto | ALLOW | ALLOW | 自动落盘，配合事后评审 |

### 文件变更评审

对标 Claude Code 事前审批 + Codex auto 模式事后评审：

- **事前**：ASK 审批弹窗展示执行前 unified diff，接受 / 拒绝 / 本会话全部接受（`a`）；拒绝时 agent 收到明确拒绝原因可调整方案。
- **事后**：AUTO 模式写操作直接落盘但登记进会话级变更台账（ChangeLedger）；turn 结束展示变更汇总；`/review` 随时查看/接受/回滚（undo 栈逐级回退，内存快照超限时走 SessionGit git 快照兜底）。无头环境 turn 结束自动接受并留痕。
- **TUI 评审面板**：turn 结束有 pending 变更时弹出阻塞式面板（↑/↓ 选择 · d 看 diff · a/r 单条裁决 · A/R 全量 · Esc 跳过）。
- **回滚通知**：revert 后向 agent 注入 `<system-reminder>` 通知，避免其基于被撤销的文件状态继续工作。
- **台账持久化**：pending 评审列表随会话落盘（review-ledger.json + 内容快照），/resume 后恢复可评审可回滚。
- **WebUI 审批桥**：`python -m dot.coding.webui` 启动服务端闭环（prompt → ASK 挂起 → HTTP 审批 → 恢复 → 事后评审 API）。
- 黑名单 DENY 不可被任何评审/确认放行。

### 三级压缩

| 级别 | 阈值 | 操作 | 需要 LLM |
| --- | --- | --- | --- |
| L1 | ≥50% | 去掉可恢复 tool 结果（read_file），保留路径 | ❌ |
| L2 | ≥70% | 删除老旧 tool 调用（bash/grep 输出） | ❌ |
| L3 | ≥85% | LLM 生成结构化摘要 | ✅ |

## 项目结构

```text
src/dot/
├── ai/                          # Layer 1: Provider 抽象
│   ├── types.py                 # 消息类型（AgentMessage, AssistantMessage...）
│   ├── provider.py              # ModelProvider Protocol
│   ├── events.py                # ProviderEvent discriminated union
│   ├── stream.py                # canonicalize_provider_stream
│   ├── catalog.py               # catalog.toml 配置加载
│   ├── limits.py                # 上下文窗口估算
│   └── providers/openai.py      # OpenAI Provider 实现
│
├── agent/                       # Layer 2: Agent 核心
│   ├── tools.py                 # AgentTool frozen dataclass
│   ├── events.py                # AgentEvent discriminated union
│   ├── harness.py               # AgentHarness（消息历史、事件订阅、双队列）
│   ├── loop.py                  # run_agent_loop（内层循环）
│   ├── executor.py              # execute_tool_safely 统一兜底
│   ├── history.py               # repair_tool_history 自愈
│   ├── cancel.py                # CancellationToken Protocol
│   └── types.py                 # AgentLoopResult, TokenUsage
│
├── workflow/                    # Layer 3: 通用工作流引擎
│   ├── graph.py                 # WorkflowGraph（线性 + 条件分支 + 执行循环）
│   ├── node.py                  # WorkflowNode Protocol / AgentNode / FunctionNode
│   ├── context.py               # WorkflowContext（data/results + 取消令牌）
│   ├── events.py                # WorkflowEvent discriminated union
│   └── cancel.py                # WorkflowCancellationToken（只读 Protocol）
│
├── coding/                      # Layer 4: Coding 应用
│   ├── state.py                 # CodingWorkflowState + ValidationResult
│   ├── workflow.py              # 内置 coding 工作流（引擎的业务实例）
│   ├── permission.py            # PermissionManager 三级拦截
│   ├── modes.py                 # AgentMode (plan/edit/auto)
│   ├── commands.py              # CommandRegistry 斜杠命令
│   ├── host.py                  # CodingHost 组装层
│   ├── tools/                   # 内置工具
│   │   ├── file_tools.py        # read_file / write_file / edit_file
│   │   ├── bash_tool.py         # bash（含危险命令检测）
│   │   ├── glob_tool.py         # glob_search
│   │   └── grep_tool.py         # grep
│   ├── session/                 # 会话管理
│   │   ├── session.py           # Session（消息 + 文件快照）
│   │   ├── manager.py           # SessionManager 生命周期
│   │   ├── storage.py           # SessionStorage（JSONL）
│   │   └── tree.py              # SessionTree 分支管理
│   ├── compress/                # 三级压缩
│   │   ├── planner.py           # 压缩规划
│   │   ├── l1_extract.py        # L1 去可恢复 tool 结果
│   │   ├── l2_summarize.py      # L2 删老旧 tool 调用
│   │   └── l3_semantic.py       # L3 LLM 摘要
│   ├── trace/                   # 事件驱动链路追踪
│   │   ├── collector.py         # TraceCollector
│   │   └── exporter.py          # JSONL 导出
│   ├── skills/                  # Skills（提示词注入）
│   │   ├── skill.py             # SKILL.md 解析
│   │   └── manager.py           # SkillManager
│   ├── extensions/              # 扩展系统
│   │   ├── api.py               # ExtensionAPI
│   │   ├── generation.py        # ExtensionGeneration liveness token
│   │   └── builtins/mcp/        # MCP 内置扩展
│   ├── ui/                      # UI 抽象
│   │   └── bridge.py            # UiBridge + NullUiBridge
│   └── cli/                     # CLI 入口
│       ├── app.py               # typer 命令
│       └── config.py            # CLIConfig
│
└── __init__.py                  # 顶层导出
```

## 关键实体

| 实体 | 所在层 | 说明 |
| --- | --- | --- |
| `ModelProvider` | ai | Protocol，单方法 `stream_response` |
| `ProviderEvent` | ai | LLM 流式事件 discriminated union |
| `AgentTool` | agent | frozen dataclass，工具定义 |
| `AgentEvent` | agent | Agent 生命周期事件 |
| `AgentHarness` | agent | 消息历史 + 事件订阅 + 双队列 |
| `AgentLoopResult` | agent | 内层循环返回值 |
| `WorkflowNode` | workflow | 节点 Protocol（AgentNode / FunctionNode 为内置实现） |
| `WorkflowGraph` | workflow | 节点编排 + 条件路由 + 执行循环 |
| `WorkflowContext` | workflow | 节点间数据容器（data / results / 取消令牌） |
| `WorkflowEvent` | workflow | 引擎生命周期事件 discriminated union |
| `CodingWorkflowState` | coding | plan→code→validate 的业务状态 |
| `PermissionManager` | coding | 三级拦截权限管控 |
| `AgentMode` | coding | plan / edit / auto |
| `Session` | coding | 消息历史 + 文件快照 |
| `TraceCollector` | coding | 事件驱动 span 树 |

## 依赖

```toml
[project]
name = "dot-agent"
version = "2.0.0-dev"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.28.1",
    "pydantic>=2.0",
    "typer>=0.16.0",
    "rich>=13.7.0",
    "mcp>=1.0",
    "prompt_toolkit>=3.0.43",
    "python-dotenv>=1.1.1",
    "pyyaml>=6.0.3",
]
```

零 langchain / langgraph / chromadb 依赖。

## 快速开始

```bash
# 安装
pip install -e .

# 配置
cp .env.example .env
# 编辑 .env 填入 API_KEY / MODEL / BASE_URL

# 运行
agent                    # TUI 交互模式
agent run "任务描述"      # 一次性执行
agent console            # 控制台模式
# 在 console / TUI 中输入 /workflow 任务，可运行 plan → code → validate
```

## 设计文档

- [架构重构 Spec](specs/004-agent-architecture-overhaul/spec.md)
