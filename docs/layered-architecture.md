# MokioClaw 分层架构

> 从用户输入到 LLM 输出，按实际职责分为 9 层。本文档是项目重构的参考基准——后续目录调整、模块迁移都以本文档的分层定义为准。

---

## 设计原则

1. **分层边界清晰**：每层只和相邻层通信，不跨层调用
2. **横切关注点独立**：安全、保障、记忆不侵入业务逻辑，通过状态对象传递
3. **用户可感知最小化**：只有交互层直接面向用户，其余层只产出事件/数据
4. **可替换性**：每层可以独立替换（如 TUI 换框架、记忆层换策略、LLM 换供应商）

---

## 总览

```
┌──────────────────────────────────────────────────────────────────┐
│  交互层 (Interaction Layer)                                        │
│  CLI (Typer + Rich) / TUI (Textual)                               │
│  职责：接收用户输入，渲染输出，是用户唯一能感知的入口                 │
├──────────────────────────────────────────────────────────────────┤
│  编排层 (Orchestration Layer)                                      │
│  orchestration/agent.py → orchestration/workflow.py                                │
│  Entry Workflow + Complex Workflow                                 │
│  职责：意图路由、节点驱动、异常处理、中断恢复                       │
├──────────────────────────────────────────────────────────────────┤
│  Agent 层 (Agent Layer)                                            │
│  planner / searchAgent / codeAgent / verifier                      │
│  职责：每个 Agent 是有独立 prompt、工具集和循环的"角色"            │
├──────────────────────────────────────────────────────────────────┤
│  工具层 (Tool Layer)                                               │
│  registry → 9 个 StructuredTool                                    │
│  职责：Agent 的"手和脚"，封装所有外部交互                           │
├──────────────────────────────────────────────────────────────────┤
│  记忆层 (Memory Layer)                                             │
│  rules / working_memory / history_summary_store                    │
│  职责：分层存放上下文，解决"上下文越长越笨"的问题                  │
├──────────────────────────────────────────────────────────────────┤
│  状态层 (State Layer)                                              │
│  MokioGraphState (graph) + RuntimeState (runtime)                  │
│  职责：两层状态容器，分别管"工作流数据"和"运行时配置"              │
├──────────────────────────────────────────────────────────────────┤
│  保障层 (Reliability Layer)                                        │
│  checkpoint / trace / retry / parallel / session                   │
│  职责：不改变 Agent 行为，但让系统可靠、可观测、可恢复             │
├──────────────────────────────────────────────────────────────────┤
│  安全层 (Security Layer)                                           │
│  approval (审批) + path_security (路径安全)                        │
│  职责：控制"Agent 能做什么不能做什么"                              │
├──────────────────────────────────────────────────────────────────┤
│  提供者层 (Provider Layer)                                         │
│  openai_provider.py → ChatOpenAI                                   │
│  职责：对 LLM 供应商的抽象，统一入口                                │
└──────────────────────────────────────────────────────────────────┘
```

---

## 第 1 层：交互层

**职责**：接收用户输入，渲染输出。用户唯一能感知到的入口。

**文件**：

| 文件 | 职责 |
|------|------|
| `interaction/app.py` | Typer CLI 入口，参数解析，启动 workflow |
| `interaction/formatter.py` | Rich 终端渲染（Panel / Table） |
| `interaction/event_summary.py` | 事件摘要数据类（TUI 用） |
| `interaction/tui/app.py` | Textual 全屏 TUI（多轮 session） |
| `interaction/tui/approval.py` | 审批弹窗（线程间同步） |
| `interaction/tui/logo.py` | PNG 半块字符 logo 渲染 |

**两种模式**：

| 模式 | 入口函数 | 特点 |
|------|---------|------|
| Rich CLI | `stream_agent_events()` | 单次任务，事件流直接打印到终端 |
| TUI | `stream_session_events()` | 多轮对话，复用同一个 workspace，事件投递到主线程 |

TUI 比 Rich CLI 多了 session 管理——每次 turn 写入 `session.json` 和 `SESSION_SUMMARY.md`，自动压缩旧轮次（保留最近 18 轮）。

**向下调用**：调用编排层的 `stream_agent_events()` / `stream_session_events()`，消费其产出的事件流。

---

## 第 2 层：编排层

**职责**：把用户意图转化为节点执行计划，驱动 LangGraph 状态图流转，处理异常和中断恢复。

**文件**：

| 文件 | 职责 |
|------|------|
| `orchestration/agent.py` | 顶层编排器，`stream_agent_events()` / `stream_session_events()` |
| `orchestration/workflow.py` | 构建 Entry Workflow 和 Complex Workflow 两个 StateGraph |

**这是整个系统的发动机**。`stream_agent_events()` 的流程：

```
1. 创建 RuntimeState（workspace / approval / checkpoint / bash 配置）
2. 运行 Entry Workflow → intent_router 判断 chat 还是 workflow
3. 如果是 workflow → 创建 CheckpointManager + TraceRecorder
4. 流式执行 Complex Workflow
5. 每个事件推送出去（custom_event / graph_event）
6. 异常处理：KeyboardInterrupt → 保存 interrupted checkpoint
7. 结束时保存 finished checkpoint + trace summary
```

**两个工作流**：

```
Entry Workflow（入口路由）
  START → intent_router → [chat → chat_responder → END]
                           → [workflow → planner → END]

Complex Workflow（主工作流）
  START → planner → context_monitor
    → [条件路由] context_compressor / verifier / planner / final
      → verifier ↔ planner（失败重试循环）
        → final → END
```

**向下调用**：调用 Agent 层的 planner 节点，节点内部再调用工具层和 Agent 层。

---

## 第 3 层：Agent 层

**职责**：每个 Agent 是一个有独立 prompt、工具集和循环的"角色"，由 planner 通过 toolcall 分派。

**文件**：

| 文件 | 职责 |
|------|------|
| `agents/search_agent.py` | searchAgent：只能用 WebSearchTool，最多 4 轮循环 |
| `agents/code_agent.py` | codeAgent：文件操作 + 命令执行 + todo 管理，最多 10 轮循环 |

**关键设计**：planner 不是直接调用 Agent 函数，而是把 `CallSearchAgentTool` 和 `CallCodeAgentTool` 注册成自己的工具。LLM 自己决定"我需要搜索"还是"我需要写代码"，再产出 tool_call。

```
planner 的 LLM 产出 tool_call: CallSearchAgentTool(instruction="...")
  → Graph 层捕获 → 执行 run_search_agent()
  → 结果作为 ToolMessage 返回给 planner
```

两个 Agent 内部各自有独立的工具调用循环，循环结束返回 `{ok, summary, messages}`。

| Agent | 可用工具 | 最多循环 |
|-------|---------|---------|
| searchAgent | WebSearchTool | 4 轮 |
| codeAgent | FileRead/Write/Edit, Glob, Grep, Bash, Notepad, TodoUpdate | 10 轮 |

**向下调用**：调用工具层的具体工具。

---

## 第 4 层：工具层

**职责**：Agent 的"手和脚"。每个工具通过 `StructuredTool.from_function()` 注册，带 name、description、参数 schema。

**文件**：

| 文件 | 职责 |
|------|------|
| `tools/registry.py` | 工具注册表，`build_tools()` / `build_read_only_tools()` |
| `tools/file_tools.py` | FileReadTool / FileWriteTool / FileEditTool |
| `tools/glob_tool.py` | GlobTool（按文件名通配符搜索） |
| `tools/grep_tool.py` | GrepTool（按正则搜索文件内容） |
| `tools/bash_tool.py` | BashTool（执行命令 + 审批 + shims + 输出管理） |
| `tools/web_search_tool.py` | WebSearchTool（Tavily） |
| `tools/todo_tool.py` | TodoWriteTool / TodoUpdateTool（TODO.md 持久化） |
| `tools/notepad_tool.py` | NotepadReadTool / NotepadAppendTool（NOTEPAD.md） |

**两套工具集**：

| 工具集 | 用途 | 包含的工具 |
|--------|------|-----------|
| `build_tools()` | planner / codeAgent | FileRead, FileWrite, FileEdit, Glob, Grep, Bash, NotepadRead, NotepadAppend, TodoUpdate |
| `build_read_only_tools()` | verifier | FileRead, Glob, Grep, Bash (read-only), NotepadRead, WebSearch |

**向下依赖**：调用安全层的审批和路径检查，调用提供者层的模型做 token 估算。

---

## 第 5 层：记忆层

**职责**：解决"上下文越长越笨"的问题，把信息分层存放，不是所有东西都塞进 messages。

**文件**：

| 文件 | 职责 |
|------|------|
| `memory/memory.py` | `build_layered_memory()` / `format_layered_memory_for_prompt()` |
| `memory/retrieval.py` | 基于意图的记忆检索触发器 |

**三层记忆**：

| 层 | 来源 | 特点 |
|----|------|------|
| rules | prompt 模板静态生成 | 跨任务稳定不变，Agent 不可改写 |
| working_memory | graph state + TODO.md | 当前任务的动态状态，每步更新 |
| history_summary_store | NOTEPAD.md + HISTORY_SUMMARY.md + context_summary | 压缩后的长期记忆，上下文溢出时重建 |

节点 prompt 不再手写拼接上下文，而是统一调用 `build_layered_memory()` 组装三层，再 `format_layered_memory_for_prompt()` 序列化为 JSON 注入。

**压缩时的记忆保留策略**：

- 用户任务、当前计划、todo、验收标准、验证命令
- searchAgent 的研究结论和来源链接
- codeAgent 的产物、重要文件和执行摘要
- verifier 的失败原因、下一步建议和风险
- workspace 内 `TODO.md` 和 `NOTEPAD.md` 中的持久上下文

**依赖关系**：读取状态层的数据，持久化到 workspace 文件（TODO.md / NOTEPAD.md / HISTORY_SUMMARY.md）。

---

## 第 6 层：状态层

**职责**：两层状态容器，分别管"运行时配置"和"工作流数据"。所有其他层都依赖状态层。

### RuntimeState（`state/runtime.py`）

贯穿整个任务执行的生命周期对象，所有工具和节点都依赖它：

```
workspace: Path                              # 工作区根目录
read_files: dict[Path, FileSnapshot]         # 已读文件快照表
approval_mode: str                           # inline / auto / deny
approval_handler: Callable                   # 审批回调函数
bash_default_timeout_seconds: int            # 默认 120s
bash_max_timeout_seconds: int                # 最大 600s
bash_max_output_chars: int                   # 默认 6000
bash_env_file: Path | None                   # 额外 env 文件
checkpoint_mode: str                         # light / strict / off
resume_from: Path | None                     # 恢复来源
trace_mode: str                              # on / off
```

关键方法：`assert_workspace_path(path, operation)` 防止路径遍历攻击，所有文件操作都必须经过此检查。

### MokioGraphState（`state/graph.py`）

LangGraph 工作流的共享状态，约 40 个字段：

```
# 任务核心
task / runtime / plan_summary / todos
acceptance_criteria / verification_commands

# 验证循环
verification_results / passed / attempts / max_attempts
verification_checks

# 意图路由
intent_route / intent_reason / intent_confidence / chat_response

# 上下文管理
context_summary / context_token_count / context_token_limit
context_should_compress / context_next_node
compression_events / memory_snapshot / history_summary

# 智能体交互
agent_handoffs / code_agent_summary / verifier_summary
last_actor_summary / research_notes / sources

# 会话管理
session_id / session_turn / session_context

# 消息与输出
messages / final_answer / last_error / metadata
```

两个状态的关系：`MokioGraphState.runtime` 字段持有 `RuntimeState` 实例，节点通过 `state["runtime"]` 访问运行时配置。

**被依赖关系**：所有上层（交互层、编排层、Agent 层、工具层、记忆层）都读写状态层的数据。

---

## 第 7 层：保障层

**职责**：不改变 Agent 行为，但让系统更可靠、可观测、可恢复。Harness Engineering 的核心。

**文件**：

| 文件 | 职责 |
|------|------|
| `reliability/checkpoint.py` | CheckpointManager — 保存/恢复 workspace 快照 |
| `reliability/trace.py` | TraceRecorder — events.jsonl / summary.json / timeline.md |
| `reliability/session.py` | SessionManager — TUI 多轮对话持久化 |
| `reliability/retry.py` | 工具调用指数退避重试 |
| `reliability/parallel.py` | 并行工具调用（自动检测依赖） |

| 模块 | 解决的问题 | 关键机制 |
|------|-----------|---------|
| checkpoint | 中断后怎么继续 | light（workspace 快照 + RECOVERY.md）/ strict（+ state.json + events.jsonl）/ off |
| trace | 运行过程可追溯 | events.jsonl（结构化日志，带序号和时间戳）+ summary.json（统计）+ timeline.md（人类可读时间线） |
| session | TUI 多轮对话 | session.json（结构化 turn/history）+ SESSION_SUMMARY.md（人可读摘要），保留最近 18 轮 |
| retry | 工具调用失败重试 | 指数退避 1s → 2s → 4s → 8s → 10s max，区分可重试/不可重试异常 |
| parallel | 工具调用加速 | 自动检测读写冲突，读多写少时并行（默认 4 并发），读写互斥时串行 |

**Checkpoint 保存时机**：开始运行 → graph update → 失败/审批类工具结果 → 中断（Ctrl+C） → 结束。

**Trace 记录内容**：run start/end、custom event、graph update、tool call/result、handoff、checkpoint。

**依赖关系**：保障层是旁路系统，由编排层初始化，在工作流运行期间持续记录，不改变主流程行为。

---

## 第 8 层：安全层

**职责**：控制"Agent 能做什么不能做什么"，分权限控制和路径安全两条线。

**文件**：

| 文件 | 职责 |
|------|------|
| `security/approval.py` | 审批系统（风险命令分类 + 人工确认） |
| `security/path_security.py` | 路径安全检查（遍历防护 + 黑名单 + 写白名单 + 敏感文件） |
| `tools/bash_tool.py` | 危险命令硬拦截（rm -rf / format / shutdown） |

### 审批系统

```
BashTool 收到命令
  → classify_command_risk() 匹配风险模式
  → 匹配到 → 创建 ApprovalRequest（含 UUID）
  → 根据 approval_mode 行为：
      inline → CLI/TUI 弹出审批弹窗，等用户 y/n
      auto   → 自动批准
      deny   → 自动拒绝
```

风险命令示例：`pip install`、`npm install`、`curl`、`wget`、`uvicorn`、`uv add` 等。

### 路径安全

```
assert_workspace_path(path, operation="read"|"write"|"delete")
  → validate_path_access()
    ├─ 路径遍历检查（禁止 ../../etc/passwd）
    ├─ 黑名单检查（禁止进入 .git / .venv / .mokioclaw / node_modules）
    ├─ 写权限检查（write/delete 只允许在 src/ tests/ docs/ examples/ scripts/）
    └─ 敏感文件检查（禁止操作 .env / .pem / .key / id_rsa）
```

异常类型：`PathTraversalError`（路径遍历攻击）、`PathAccessDeniedError`（黑名单/写权限拒绝）。

**被依赖关系**：工具层（BashTool、FileRead/Write/Edit）在每次操作前调用安全层做检查。

---

## 第 9 层：提供者层

**职责**：对 LLM 供应商的抽象，所有节点和 Agent 通过统一入口获取模型。

**文件**：

| 文件 | 职责 |
|------|------|
| `providers/openai_provider.py` | `create_model()` → ChatOpenAI，支持超时和重试配置 |

```
create_model() → ChatOpenAI(api_key, model, base_url, temperature=0)
```

环境变量控制：

```
MOKIO_REQUEST_TIMEOUT=120    # 请求超时（秒），默认 120
MOKIO_MAX_RETRIES=3          # 重试次数，默认 3
```

首次调用时校验 `API_KEY`、`MODEL`、`BASE_URL` 三个必需环境变量，缺失则抛 `RuntimeError`。

**被依赖关系**：编排层、Agent 层、工具层（GrepTool 的 ripgrep 检测等）都可能调用。

---

## 层间依赖关系

```
交互层
  └→ 编排层
       └→ Agent 层
            └→ 工具层
                 ├→ 安全层（审批 + 路径检查）
                 └→ 提供者层（模型调用）
            
状态层 ← 所有上层（读写状态数据）
记忆层 ← Agent 层 / 编排层（注入 prompt + 持久化）
保障层 ← 编排层（初始化 + 旁路记录）
```

**横切关注点**（被多层引用，不属于任何单一上层）：

```
状态层   ← 几乎所有模块都用
安全层   ← 工具层、交互层（审批弹窗）
记忆层   ← Agent 层、编排层
保障层   ← 编排层初始化，运行时旁路记录
提供者层 ← 编排层、Agent 层、工具层
```

---

## 各层文件清单

| 层 | 目录 | 文件 |
|----|------|------|
| 交互层 | `interaction/` | `app.py`, `formatter.py`, `event_summary.py` |
| 交互层 | `interaction/tui/` | `app.py`, `approval.py`, `logo.py` |
| 编排层 | `orchestration/` | `agent.py`, `workflow.py`, `nodes.py`, `agent_loop.py` |
| Agent 层 | `agents/` | `search_agent.py`, `code_agent.py` |
| 工具层 | `tools/` | `registry.py`, `bash_tool.py`, `file_tools.py`, `glob_tool.py`, `grep_tool.py`, `web_search_tool.py`, `todo_tool.py`, `notepad_tool.py` |
| 记忆层 | `memory/` | `memory.py`, `retrieval.py`, `tiered_compression.py`, `dual_threshold_compression.py`, `tool_disclosure.py` |
| 状态层 | `state/` | `runtime.py`, `graph.py` |
| 保障层 | `reliability/` | `checkpoint.py`, `trace.py`, `session.py`, `retry.py`, `parallel.py` |
| 安全层 | `security/` | `approval.py`, `path_security.py` |
| 提供者层 | `providers/` | `openai_provider.py` |

---

## 跨层数据流

```
用户输入
  │
  ▼
[交互层] CLI/TUI 解析参数，启动 stream_agent_events()
  │
  ▼
[编排层] 创建 RuntimeState（状态层），初始化 Checkpoint（保障层）+ Trace（保障层）
  │
  ▼
[编排层] Entry Workflow → intent_router 判断 chat/workflow
  │
  ▼
[编排层] Complex Workflow 启动
  │
  ▼
[Agent 层] planner 产出 tool_call
  │  读取记忆层的三层记忆注入 prompt
  │  读写状态层的 MokioGraphState
  │
  ▼
[工具层] 执行工具（FileRead / Bash / WebSearch ...）
  │  每次操作前调用安全层做审批和路径检查
  │  读写状态层的 RuntimeState（workspace / read_files）
  │
  ▼
[保障层] 持续记录 checkpoint / trace / session 事件
  │
  ▼
[提供者层] ChatOpenAI 完成模型调用
  │
  ▼
输出事件 → [交互层] Rich/TUI 渲染
```

---

## 重构参考

> 本文档是后续目录重构的依据。如需调整目录结构，原则如下：
>
> 1. 每层一个子目录（如 `src/mokioclaw/interaction/`、`src/mokioclaw/orchestration/`）
> 2. 横切关注点（安全、保障、记忆）保持独立目录，不被任何业务层吞并
> 3. `providers/` 始终在最底层，不依赖其他任何层
> 4. 状态层的数据类定义保持稳定，是各层之间的"契约"
