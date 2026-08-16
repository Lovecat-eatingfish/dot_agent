# dot_agent vs Claude Code 功能差异分析

> 生成时间：2026-08-16
> 基于对 dot_agent (mokioclaw) 源码精读 + Claude Code 官方文档/实现调研

---

## 概览

dot_agent (mokioclaw) 是一个 Python + LangGraph 实现的多智能体 CodeAgent，大量功能设计直接标注"对齐 Claude Code"。Claude Code 是 Anthropic 的生产级 TypeScript CLI 工具。两者架构理念相似，但成熟度和生态广度差距明显。

---

## 1. Agent 架构与循环

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 编排模型 | LangGraph StateGraph：planner → searchAgent/codeAgent → verifier → repair 循环 | 单一主 agent loop + Task 工具派生子 agent，无独立 planner/verifier 节点 |
| 循环实现 | `run_agent_loop` in `orchestration/agent_loop.py`，max_loops 控制 | 内置 query loop，maxTurns 控制 |
| 恢复链 | token 预算追踪 + max_output_tokens 升级恢复(8k→64k) + 413 prompt-too-long force_compact | auto-compact 在 95% 容量触发，BudgetTracker 类似机制 |

- **dot_agent 独有**：planner → verifier → repair 显式流水线
- **Claude Code 独有**：1M token 上下文窗口、effort 级别调整 (`/effort` xhigh/high/medium)

---

## 2. 工具集 (Tools)

| 工具 | dot_agent | Claude Code |
|------|-----------|-------------|
| 文件读 | `FileReadTool` | `Read` |
| 文件写 | `FileWriteTool` | `Write` |
| 文件编辑 | `FileEditTool` (old_text → new_text 单段替换) | `Edit` (单段) + `MultiEdit` (多段批量替换) |
| Bash | `BashTool` | `Bash` |
| Glob | `GlobTool` | `Glob` |
| Grep | `GrepTool` (ripgrep + Python fallback) | `Grep` (ripgrep) |
| Web 搜索 | `WebSearchTool` (Tavily) | `WebSearch` (Anthropic 自有后端) |
| 网页抓取 | ❌ 无 | `WebFetch` (URL→Markdown，小模型提取，15 分钟缓存) |
| Todo | `TodoUpdateTool` (增量更新单条) | `TodoWrite` (全量写入) + `TaskCreate/TaskGet/TaskUpdate/TaskList` (TodoV2) |
| 子 Agent | `AgentTool` (`tools/agent_tool.py`) | `Task` / `Agent` |
| 笔记本 | ❌ 无 | `NotebookEdit` / `NotebookRead` |
| ExitPlanMode | ❌ 无独立工具 | `ExitPlanMode` |
| AskUser | ❌ 无 | `AskUserQuestion` |
| Skill | `SkillTool` | `Skill` |

- **Claude Code 独有**：`WebFetch`、`MultiEdit`、`NotebookEdit`、`ExitPlanMode`、`AskUserQuestion`、`ListMcpResources`/`ReadMcpResource` 作为一等工具
- **dot_agent 独有**：`ToolSearchTool`（工具发现）、`LoadMcpTool`（延迟加载 MCP 工具）

---

## 3. 权限与安全模型

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 模式 | `agent_mode`: auto/plan/approve/edit/bypass (`security/agent_mode.py`) | 6 种：default/acceptEdits/plan/auto/dontAsk/bypassPermissions |
| 审批 | `approval_mode`: inline/auto/deny | 与 permission mode 合并 |
| 沙箱 | 轻量级 workspace 路径约束 (`security/sandbox.py`)，无 OS 级隔离 | macOS seatbelt / Linux landlock OS 级沙箱，网络规则可配置 |
| 分类器 | `yoloClassifier` 两段式（规则 + LLM），默认关闭需 `MOKIO_AUTO_CLASSIFIER=1` | auto mode 用 Sonnet 4.6 背景分类器，每个工具调用都过 |
| 危险命令 | 正则模式列表 (`DANGEROUS_PATTERNS` in `bash_tool.py`) | 内置只读命令集 + 分类器 |

- Claude Code 的沙箱是真正的 OS 级隔离（macOS seatbelt sandbox-exec），dot_agent 只是路径字符串检查。这是最大的安全差距。
- dot_agent 的 `agent_mode` 设计更细粒度（edit 模式禁 bash 但允许文件读写），但缺少 Claude Code 的 `acceptEdits` 和 `dontAsk` 模式。

---

## 4. 上下文管理 / 压缩

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 压缩策略 | 双阈值：soft 70% 增量压缩 / hard 90% 全量压缩 (`memory/dual_threshold_compression.py`) | auto-compact 在 95% 触发 |
| 摘要链 | `SummaryChain` 增量叠加（O(n)） | 一次性全量摘要 |
| 持久化 | 压缩前写 `RAW_HISTORY.md` | 原始消息保留在 session transcript |
| 手动压缩 | `/compact` 斜杠命令 | `/compact` + `/rewind` 中的 "summarize from here/up to here" |
| L4 应急 | `force_compact_messages` 保留 system + 最近 10 条 | 类似 reactive compact |

- dot_agent 的双阈值 + 增量摘要链设计比 Claude Code 更精细（面试导向的工程化），但 Claude Code 有更成熟的 `/rewind` + 定点摘要能力。

---

## 5. 记忆系统

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 层级 | 三层：rules / working_memory / history_summary (`memory/memory.py`) | CLAUDE.md 层级（managed > user > project）+ auto-memory |
| 索引化 | 启动仅加载索引 ~200 tokens，按需读取完整文件 | 无显式索引化 |
| 渐进式披露 | ✅ | ❌ |
| 配置文件 | `~/.mokioclaw/CLAUDE.md` + `.mokioclaw/config.md` (YAML frontmatter) | `CLAUDE.md` (project) + `~/.claude/CLAUDE.md` (user) + managed settings |
| Session Memory | ❌ | Anthropic API 上的 Session Memory（Pro/Max）使 `/compact` 瞬时 |

- dot_agent 的三层记忆 + 索引化渐进披露是它最亮眼的差异化设计，比 Claude Code 的 flat CLAUDE.md 更工程化。

---

## 6. 子 Agent (Subagents)

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 实现 | `AgentTool.fork_subagent` (`tools/agent_tool.py`) | `Task` 工具 |
| 上下文隔离 | ✅ 独立 messages | ✅ 独立 context window |
| 嵌套深度 | `MOKIO_MAX_SUBAGENT_DEPTH=3` | 类似深度限制 |
| 后台执行 | `run_in_background=true` + `background_tasks.py` | `run_in_background` |
| 取消 | `_abort` 协作式 | `TaskStop` 工具 |
| 上下文传递 | `model=inherit` 复用父 system 前缀 | 显式 `prompt` 参数 |
| 子 Agent 配置 | ❌ 无 YAML 定义文件 | `.claude/agents/*.yaml` 可定义子 agent 的 tools/model/prompt |
| Agent Teams | ❌ | ✅ 多个独立 session 共享 tasks + peer-to-peer messaging |

- Claude Code 有可声明的子 agent 定义文件和 agent teams 协调机制，dot_agent 的子 agent 全是程序化 fork。

---

## 7. Hooks 系统

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 事件 | PreToolUse/PostToolUse/PostToolUseFailure/SessionStart/SessionEnd/UserPromptSubmit/PreCompact/Stop/SubagentStop/StopFailure (`core/hooks.py`) | PreToolUse/PostToolUse/SessionStart/SessionEnd/Stop/SubagentStop/Notification/UserPromptSubmit/PreCompact |
| Hook 类型 | Python callable | shell script / HTTP request / prompt injection / subagent |
| 阻断能力 | ✅ PreToolUse 可 block + 修改 args | ✅ 同 |
| 配置 | 代码内注册 | `settings.json` + `.claude/hooks/` |

- 事件覆盖度非常接近。dot_agent 用 Python 函数注册，Claude Code 用外部脚本/HTTP，后者更通用但前者更易开发。

---

## 8. MCP 集成

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 传输 | stdio + HTTP/SSE (`mcp/transport.py`) | stdio + SSE + streamable HTTP |
| 桥接 | `MCPBridge` 多 server 管理 (`mcp/bridge.py`) | 内置多 server |
| 沙箱 | `SandboxPolicy` workspace 约束 | OS 级沙箱 + 网络规则 |
| 工具发现 | `LoadMcpTool` 延迟加载 + `select_mcp_tools_for_bind` | 自动注册 |
| Resources | `MCPResource` + `extract_content_parts` (`mcp/protocol.py`) | `ListMcpResources` / `ReadMcpResource` 工具 |
| Disclosure | `mcp/disclosure.py` 选择性绑定 | ✅ |
| 配置 | 代码内 register_server | `.mcp.json` + settings scopes (local > project > user) |

- dot_agent 的 MCP 实现相当完整，甚至有 `disclosure.py` 做选择性工具绑定。但缺少 Claude Code 的 `.mcp.json` 声明式配置和 scope 优先级体系。

---

## 9. Checkpoint / Session Resume

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 模式 | light/strict/off (`reliability/checkpoint.py`) | 自动（每次 user prompt 创建 checkpoint） |
| 内容 | light=状态摘要 / strict=完整 graph state | 文件快照 + 对话状态 |
| 版本化 | ✅ 快照枚举 + rollback | ✅ |
| Session | `session.py` + `session_store.py`，`--resume` 恢复 | `--continue` / `--resume` / `--fork-session` |
| Rewind | ❌ 无 UI rewind | `/rewind` (Escape×2) + 定点 summarize |
| Git 集成 | `checkpoint.py` 中有 `GIT_DIR` 常量但未见完整实现 | 文件改动追踪 + git 快照回退 |

- dot_agent 有 light/strict 两级 checkpoint 模式，概念清晰。Claude Code 的 checkpoint 更深——它追踪每次文件编辑并能在 UI 中交互式回退。

---

## 10. Slash Commands & Skills

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 系统命令 | /help /clear /memory /compact /cost /model /mode /plugin /new (`interaction/commands.py`) | /init /compact /context /review /security-review /model /effort 等 |
| 自定义命令 | `.mokioclaw/commands/*.md` | `.claude/commands/*.md` |
| Skills | `SkillTool` + `discover_skills` 从 `~/.mokioclaw/skills` 加载 SKILL.md | 完整 skill 体系 + `disable-model-invocation` + `context: fork` + `allowed-tools` |
| Skill 命名空间 | ❌ | ✅ 插件 skill 有命名空间 (如 `/plugin:skill`) |

- 基本对齐。Claude Code 的 skill 有更多 frontmatter 选项（`allowed-tools`、`context: fork`、`disable-model-invocation`）。

---

## 11. Plugins

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 结构 | `plugin.json` + skills/ + commands/ (`plugins/marketplace.py`) | plugin.json bundling skills+hooks+subagents+commands+MCP+output styles |
| 内置插件 | `code-review-kit`（review-diff 命令 + review skill） | 官方 + 社区生态 |
| Marketplace | `marketplace.py` enabled_plugin_paths | ✅ |

- dot_agent 有插件框架和 marketplace 机制，但只内置一个示例插件。Claude Code 的插件是完整的打包分发单元。

---

## 12. 交互界面

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| CLI | Rich CLI（单次任务事件时间线）(`interaction/app.py`) | 交互式终端 |
| TUI | Textual TUI 多轮对话 (`interaction/tui/app.py`) | 无独立 TUI（终端本身就是交互式） |
| IDE | ❌ | VS Code / JetBrains 扩展 |
| Web | ❌ | Claude Code on the web |
| SDK | ❌ | Claude Agent SDK (programmatic) |
| Desktop | `desktop/widget.py` 桌面 widget（实验性） | Claude Desktop |

- dot_agent 有 Textual TUI（Claude Code 没有），但缺少 IDE 插件、Web 版和 SDK。

---

## 13. RAG 系统

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 实现 | 完整 RAG pipeline (`rag/` 目录)：embedding/splitter/retrieval/reranker/reorder/self_query/query_transform/guardrails/security/store/trace | ❌ 无内置 RAG |

- **dot_agent 独有**：完整的 RAG 系统，包含 ChromaDB 向量存储、BM25、reranker、query transform、self-query、guardrails。Claude Code 完全没有这个能力。

---

## 14. 其他差异

| 维度 | dot_agent | Claude Code |
|------|-----------|-------------|
| 并行工具调度 | `reliability/parallel.py` 自动检测文件级依赖，只读工具并行 | ✅ |
| Token Budget | `BudgetTracker` + 收益递减检测（连续 3 次增量 < 500） | BudgetTracker |
| Trace | `reliability/trace.py` 完整执行追踪 | ❌ 无独立 trace |
| Daemon | `daemon/manager.py` + `scheduler.py` 定时任务 | CronCreate/CronDelete/CronList |
| LLM Provider | OpenAI 兼容 API（ChatOpenAI），支持 fallback | Anthropic 原生 + Bedrock + Vertex |
| 模型 | 任意 OpenAI 兼容模型 | Claude 系列（Opus/Sonnet/Haiku） |

---

## 总结

### dot_agent 相对 Claude Code 的优势

- 显式 planner → verifier → repair 流水线（更结构化）
- 三层记忆 + 索引化渐进披露（比 flat CLAUDE.md 更精细）
- 双阈值压缩 + 增量摘要链（比一次性 compact 更工程化）
- 完整 RAG 系统（Claude Code 完全没有）
- 执行 Trace 系统
- Textual TUI 交互界面

### dot_agent 相对 Claude Code 的差距

- 无 OS 级沙箱（只有路径字符串检查，安全差距大）
- 缺 `WebFetch`（网页抓取 + 小模型提取）
- 缺 `MultiEdit`（批量编辑）
- 缺 `NotebookEdit`（Jupyter 支持）
- 缺 `ExitPlanMode` / `AskUserQuestion` 交互工具
- 无 IDE 集成 / Web 版 / Agent SDK
- 沙箱无网络规则配置
- 子 agent 无声明式 YAML 定义
- 无 agent teams 协调
- 无 `/rewind` 交互式回退
- 模型绑定单一（OpenAI 兼容），无 Anthropic 原生/Bedrock/Vertex
- 插件生态刚起步

### 已对齐的核心能力

工具集基础功能、hooks 事件覆盖度、MCP 多传输、subagent 上下文隔离 + 深度限制 + 后台执行、token budget 追踪、斜杠命令、skills、checkpoint、session resume。
