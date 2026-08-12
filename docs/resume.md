# dot_agent — 简历描述

> 以下为不同长度的版本，按需选用。

---

## 一句话版本（用于简历项目列表）

**dot_agent**：从零搭建的 Mini CodeAgent，基于 LangGraph 实现多智能体协作工作流，支持上下文自动压缩、三层记忆系统、检查点回退与并行工具调度。

---

## 标准版本（推荐，~200 字）

**dot_agent** — 从零开始构建的多智能体 AI Agent 系统，对标 Claude Code / Codex 的核心能力。

- **多智能体编排**：基于 LangGraph StateGraph 设计 planner → searchAgent / codeAgent → verifier 的协作流水线，planner 以 JSON 路由决策替代传统 tool-calling supervisor，节点间通过结构化状态解耦。
- **上下文工程**：实现双阈值上下文压缩策略（soft 70% / hard 90%），支持全量压缩与增量叠加两种模式；消息按重要性分为四级（KEEP / LIGHT / HEAVY / DROP）分级处理，在 token 受限场景下维持长对话能力。
- **三层记忆系统**：rules（持久规则）+ working_memory（当前任务）+ history_summary_store（历史摘要），支持记忆索引化与渐进式披露，减少不必要的历史上下文加载。
- **可靠性机制**：检查点轻量/严格双模式持久化，支持版本化快照与 rollback 回退；EventBus 钩子系统实现事件订阅/过滤/异步处理；并行工具调用自动检测文件级冲突，TodoUpdate 强制串行避免竞态。
- **工程化**：9 层分层架构、YAML frontmatter + markdown body 的用户配置系统（动静分离提示词）、Rich CLI + Textual TUI 双交互模式，212 个单测覆盖核心路径。

---

## 详细版本（用于简历项目描述 / 技术博客 / GitHub README）

### 项目概述

dot_agent 是一个教学优先的 Mini CodeAgent，从 ToolCall 到 Claw，完整展示了一个 AI Agent 系统从工具调用到多智能体协作的演进路径。项目以 LangGraph 为编排核心，实现了生产级 Agent 所需的多项关键能力。

### 核心架构

采用 **9 层分层架构**（Interaction → Orchestration → Agent → Tool → Memory → State → Reliability → Security → Provider），每层职责单一、通过接口隔离，便于独立测试与扩展。

工作流基于 LangGraph StateGraph 构建，核心流程：

```
intent_router → planner → searchAgent / codeAgent → context_monitor → verifier → final
                                                         ↑                        |
                                                         +────── repair ←─────────+
```

### 关键技术实现

#### 1. 多智能体协作（Multi-Agent Orchestration）

- Planner 节点以 **JSON 路由决策** 替代传统 tool-calling supervisor，输出 `route`（search/code/verify/final/replan/repair）+ `route_instruction`，由图的条件边路由到对应执行节点。
- searchAgent / codeAgent / verifier / repair 作为图上的独立节点，通过 `MokioGraphState` 共享状态，planner 不阻塞等待子 Agent 返回。
- Repair 循环：verifier 失败时生成 `recommended_next_instruction`，路由到 repair 节点委派 codeAgent 修复，再回到 verifier 校验，形成闭环。

#### 2. 上下文工程（Context Engineering）

- **双阈值压缩**：`soft_threshold=70%` 触发增量压缩（保留 20 条最近消息 + 叠加旧摘要），`hard_threshold=90%` 触发全量压缩（清空消息 + 保留 10 条 + 新摘要）。
- **分级消息处理**：按消息类型分为四级 — KEEP_ALWAYS（用户指令）、COMPRESS_LIGHTLY（普通工具结果）、COMPRESS_HEAVILY（长输出降级为摘要）、DROP（空消息）。
- **持久化历史**：压缩前将完整消息列表追加写入 `RAW_HISTORY.md`，不发送给模型但保留审计与重建能力。

#### 3. 三层记忆系统（Layered Memory）

| 层级 | 内容 | 生命周期 | 用途 |
|------|------|----------|------|
| rules | 用户自定义规则 | 跨任务持久化 | 行为约束 |
| working_memory | 当前任务关键信息 | 单任务内 | 活跃工作上下文 |
| history_summary_store | 历史对话压缩摘要 | 跨轮次 | 长期上下文回溯 |

配合 `memory_index.json` 实现记忆索引化与渐进式披露——启动时仅加载索引（~200 tokens），按需读取完整文件。

#### 4. 提示词动静分离（Static/Dynamic Prompt Separation）

参考 Claude Code memory 文件模式，将提示词拆分为三层：

- **静态层**：`prompts/agent_prompt.py` 中的角色定义与规则模板
- **动态层**：`~/.mokioclaw/CLAUDE.md` + `.mokioclaw/config.md`（YAML frontmatter + markdown body），用户自定义指令自动注入所有 Agent 的 system prompt 末尾
- **运行时层**：task / plan / memory 由各节点通过 HumanMessage 在调用时注入

#### 5. 可靠性与回退（Reliability & Rollback）

- **检查点系统**：轻量模式保存状态摘要 + 元数据，严格模式保存完整 `state.json`；支持 `list_checkpoints()` 枚举历史版本，`rollback_to_checkpoint()` 回退到任意版本并恢复 workspace 文件。
- **钩子系统**：EventBus 实现 publish/subscribe 模式，内置 TuiPushHook / CliPrintHook / CheckpointHook / TraceHook 四个开箱即用的事件处理器。
- **并行工具调度**：`execute_tool_calls()` 自动检测工具间文件级依赖，独立调用并行执行（ThreadPoolExecutor），有依赖的串行执行；TodoUpdate 强制串行避免竞态。

#### 6. 工程化特性

- **安全**：路径白名单/黑名单、路径遍历防护、高风险命令人工审批（inline/auto/deny 三模式）。
- **双交互模式**：Rich CLI（单次任务事件时间线）与 Textual TUI（多轮对话全屏界面）。
- **测试**：212 个单测覆盖核心路径，包括配置加载、提示词构建、检查点、并行工具、压缩策略等。

### 技术栈

| 层面 | 选型 |
|------|------|
| LLM 编排 | LangChain + LangGraph |
| 终端 CLI | Typer + Rich |
| 终端 TUI | Textual |
| Web 搜索 | Tavily |
| 包管理 | uv + Hatchling |
| 测试 | pytest |

### 项目地址

`github.com/enquan/dot_agent`（私有，可替换为实际地址）

---

## 极简版本（用于简历技能列表 / 一句话介绍）

基于 LangGraph 的多智能体 CodeAgent 系统，实现了多 Agent 协作编排、双阈值上下文压缩、三层记忆、检查点回退与并行工具调度，212 个单测覆盖核心路径。
