# MokioClaw 对齐缺口清单

> 对照 `docs/deep-dive-claude-code-v1.0.md`（9359 行，25 章 + 2 附录）与 MokioClaw 源码，按"对齐价值"降序分组的差异清单，供后续做架构对齐用。
>
> 生成时间：2026-08-14
> 方法：子 agent 逐章读 deep-dive 文档 + 源码交叉核对，每条缺口附文档行号佐证。

## 已对齐项（不再重复报告）

引擎层恢复链（BudgetTracker / OutputTokenRecovery / PromptTooLongRecovery / filter_unresolved_tool_uses / invoke_with_fallback / model_with_max_tokens）· Hook 系统（7 种事件 + command/http/prompt 三种 type）· 动静分离 PromptBuilder（`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`）· MCP http/SSE 传输 + 渐进披露 + resources 目录 · handoff 分类器（classify_handoff）· ToolSearch 延迟加载 · microcompact + snip 两层规则压缩 · AutoMemory + AutoDream · contextModifier（cd/env 注入）· 后台任务注册表 · checkpoint light/strict 双模式 · MCP `list_resources` / `read_resource` · `filter_unresolved_tool_uses` 悬空清洗。

---

## 关键架构提醒

**最大偏差**：MokioClaw 用 LangGraph `StateGraph` 管状态，Claude Code 用自研 `QueryEngine` 状态机。导致 GAP-2（StreamingToolExecutor）和 GAP-5（JSONL Transcript）难直接对齐 —— LangGraph 的 `add_messages` reducer 和 `stream_mode` 已提供部分能力，但也限制自定义调度器空间。对齐 GAP-2 时建议做"教学版 TrackedToolExecutor"，不完整复制 AsyncGenerator 管线。

---

## 高价值组（9 项 — Claude Code 最核心的工程思想）

### GAP-1: `isConcurrencySafe` 输入驱动判定
- **Claude Code 机制**（第 9 章 9.2，行 3013-3065）：BashTool 的 `isConcurrencySafe(input)` 接收输入参数，调用 `isReadOnly(input)` → `checkReadOnlyConstraints(input)`，**同一工具不同命令得到不同判定**：`ls -la`→`true`（可并行），`git push`→`false`（独占）。调度器 `canExecuteTool` 据此决定并行 vs 串行。注册时声明默认值，运行时按输入动态覆盖。
- **MokioClaw 现状**（`src/mokioclaw/tools/registry.py:26-52`）：`TOOL_CONCURRENCY_META` 是工具名→布尔常量字典，BashTool 恒 `False`。`is_tool_concurrency_safe(name)` 只查表，不接收 args。判定粒度是"工具名级"而非"调用级"。
- **差距类型**：部分（有并发判定框架，缺输入驱动语义）
- **对齐价值**：高 — 并发调度设计核心，体现"输入驱动的属性推断"
- **对齐难度**：中 — `is_tool_concurrency_safe` 签名改为 `(name, args)`，BashTool 复用已有 `READ_ONLY_ALLOWED` 判定第一个 token；`parallel.py` 的 `are_tools_independent` 传入 args

### GAP-2: StreamingToolExecutor 状态机调度器（TrackedTool 四态生命周期）
- **Claude Code 机制**（第 9 章 9.1，行 2909-3011）：`StreamingToolExecutor` 维护 `TrackedTool` 列表，四态 `queued → executing → completed → yielded`。`canExecuteTool` 在**入队时**计算 `isConcurrencySafe`，`processQueue` 按"非并发安全工具按序、并发安全工具可插队"调度。结果与进度分两通道（`results` vs `pendingProgress`），进度不进结果缓冲。
- **MokioClaw 现状**（`src/mokioclaw/reliability/parallel.py:36-105`）：`execute_tools_in_parallel` 用 `ThreadPoolExecutor + as_completed` 一次性提交，无四态追踪，结果按 `future_to_index` 整体返回，无增量产出，进度流完全没有。
- **差距类型**：部分（有并行执行，无状态机调度和进度流）
- **对齐价值**：高 — "状态机驱动的事件流调度"+"进度与结果分离"
- **对齐难度**：高 — 重构工具执行管线为生成器/异步流；可做教学版简化
- **建议**：实现 `TrackedToolExecutor` 类，维护 `list[TrackedTool]`，`processQueue` 用 `asyncio`/`concurrent.futures` 增量 yield 已完成结果

### GAP-3: `maxResultSizeChars` 每工具阈值 + 磁盘持久化
- **Claude Code 机制**（第 9 章 9.3，行 3067-3131）：**每个工具**声明 `maxResultSizeChars`（FileRead=Infinity 不落盘避免循环，Bash=30000，Grep=20000，FileEdit=100000）。`getPersistenceThreshold` 三层优先级：Infinity 硬豁免 → GrowthBook 远程覆盖 → `min(declared, global_cap)`。超阈值 `persistToolResult` 用 `writeFile(flag='wx')` 独占创建落盘文件，返回预览+路径。
- **MokioClaw 现状**（`src/mokioclaw/core/tool_result_budget.py:33-87`）：`ToolResultBudget` 全局单一阈值 `max_chars=50000`，所有工具共用。BashTool 另有 `DEFAULT_MAX_OUTPUT_CHARS=6000` 截断（`bash_tool.py:756-772`）落盘 `.mokioclaw/bash-outputs/`。两层独立，无"每工具声明"和"Infinity 豁免"语义。
- **差距类型**：部分（有落盘，无每工具阈值和优先级链）
- **对齐价值**：高 — "三层防御纵深"+"工具自治声明"
- **对齐难度**：**低** — `registry.py` 加 `TOOL_MAX_RESULT_CHARS` 字典，`ToolResultBudget.apply` 查表优先用工具声明值
- **建议**：`TOOL_MAX_RESULT_CHARS = {"FileReadTool": float("inf"), "BashTool": 30000, "GrepTool": 20000, ...}`，apply 先查表再回退全局默认

### GAP-4: ContentReplacementState 全局工具结果预算（跨轮替换）
- **Claude Code 机制**（第 9 章 9.5，行 3192-3253）：`ContentReplacementState = {seenIds: Set, replacements: Map<id, str>}`。每次 API 调用前 `enforceToolResultBudget` 把旧大工具结果**原地替换**为摘要标记，三分类：已替换（重放）、冻结（见过但不替换）、新增（按剩余预算和结果大小决定）。子代理 fork 时 `cloneContentReplacementState` 复制做**相同替换决策**命中 prompt cache。
- **MokioClaw 现状**（`src/mokioclaw/memory/microcompact.py:46-121`）：`microcompact_messages` 做了"过期 FileRead 结果替换"+"超大字段截断"，但**一次性全量扫描**，无 `seenIds`、无三分类、无跨调用增量决策、无子代理继承替换状态。
- **差距类型**：缺失（有微压缩，无 ContentReplacementState 预算管理器）
- **对齐价值**：高 — "全局预算"+"决策一致性保缓存命中"
- **对齐难度**：中 — 新建 `ContentReplacementState` 类，在 agent_loop 每次 invoke 前调用
- **建议**：新建 `core/content_replacement.py`，`enforce_budget(messages, budget)` 扫描 ToolMessage 按 Claude Code 三分类替换；`agent_loop.py` 的 `_invoke_with_recovery` 前调用

### GAP-5: JSONL Transcript 增量持久化 + `parentUuid` 消息链 + 侧链录制
- **Claude Code 机制**（第 5 章 5.4，行 1632-1682；第 11 章 11.2.3，行 3837-3847）：`recordTranscript` 增量写 JSONL，每条消息含 `uuid`+`parentUuid` 形成因果链。用户消息 API 调用**前** `await` 写入（崩溃恢复），assistant 消息 `fire-and-forget`。子代理消息 `recordSidechainTranscript` 录到**独立侧链**不污染主对话。`--resume` 用 `byUuid` map + `leafUuids` 重建主链。
- **MokioClaw 现状**（`src/mokioclaw/reliability/session.py:53-90`）：`append_user_turn`/`append_assistant_turn` 把**截断 content** 存入 `session.json` 的 `recent_turns` 数组。无 `uuid`/`parentUuid`，无 JSONL 逐条写入，无侧链概念。`checkpoint.py` 的 `events.jsonl` 只记录**事件**不记录**对话消息**。子代理消息直接 append 主 messages。
- **差距类型**：缺失（有 session 存储，非 transcript 模式）
- **对齐价值**：高 — "append-only 崩溃安全"+"因果链分支"+"侧链隔离"
- **对齐难度**：高 — 重构 session 持久化为 JSONL transcript；可做教学版
- **建议**：新建 `reliability/transcript.py`，`record_transcript(messages, session_id, parent_uuid)` 逐条 append JSONL；`BaseMessage` 序列化加 `uuid`/`parent_uuid`；`--resume` 读 JSONL 重建 messages

### GAP-6: 工具结果磁盘持久化的"模型可见预览+路径"消息格式
- **Claude Code 机制**（第 9 章 9.3，行 3120-3131）：`buildLargeToolResultMessage` 标准格式：`<persisted_output>\nOutput too large (XX KB). Full output saved to: {filepath}\n\nPreview (first 2KB):\n{preview}\n</persisted_output>`。模型看到预览后可主动用 FileReadTool 读完整文件。
- **MokioClaw 现状**（`src/mokioclaw/core/tool_result_budget.py:82-87`）：落盘后返回 `{"_full_output_path": str, "_truncated": True, "_original_chars": int}` + 截断预览，无标准化"模型可读"消息格式，预览逻辑用 `_make_preview` 把每字段截断到 `PREVIEW_CHARS=2000`，与"前 N 字节预览"语义不同。
- **差距类型**：偏差（有落盘，消息格式不对齐）
- **对齐价值**：中 — 格式对齐后模型行为更可预测
- **对齐难度**：**低** — 改 `ToolResultBudget._make_preview` 为单字段前缀预览
- **建议**：统一预览为 `{"ok": True, "content": preview, "_full_output_path": path, "_truncated": True}` + 标准化提示文本

### GAP-7: Bash 命令语义分析器（命令替换检测 / 引号状态机 / Zsh 危险命令）
- **Claude Code 机制**（第 14 章，行 4757-4924）：`extractQuotedContent` 三视图状态机（`withDoubleQuotes`/`fullyUnquoted`/`unquotedKeepQuoteChars`）。`COMMAND_SUBSTITUTION_PATTERNS` 检测 `$()` `${}` `<()` `=()` Zsh `=cmd` 等。`ZSH_DANGEROUS_COMMANDS` 覆盖 `zmodload`/`emulate -c`/`ztcp`。五层路径校验含 TOCTOU 防护（拒绝 `~root`/`$VAR`/`$(cmd)` 写操作）。`sed -i` 特殊检测。
- **MokioClaw 现状**（`src/mokioclaw/tools/bash_tool.py:48-66`）：`DANGEROUS_PATTERNS` 简单正则列表（`rm -rf`/`format`/`dd`/`eval`/`pipe to sh`），无引号解析、无命令替换检测、无 Zsh 特殊命令、无 TOCTOU 防护。`sandbox.py` 做绝对路径边界检查但无 shell 展开检测。`READ_ONLY_ALLOWED` 简单白名单集合。
- **差距类型**：部分（有危险命令检测，无语义分析）
- **对齐价值**：高 — "安全是设计的起点，不是正则补丁"
- **对齐难度**：中 — 可分层实现，先引号提取 + 命令替换检测
- **建议**：新建 `security/bash_analysis.py`，`extract_quoted_content(command)` 三视图；`detect_command_substitution(command)` 检测 `$()`/`${}`/`<()`；`run_bash` 调用增强 `_looks_dangerous`

### GAP-8: 权限规则系统（多源合并 + `ruleContent` 精细匹配 + 持久化）
- **Claude Code 机制**（第 13 章 13.2-13.3，行 4555-4595）：`PermissionRule = {source, ruleBehavior: 'allow'|'deny'|'ask', ruleValue: {toolName, ruleContent?}}`。`ruleContent` 精细匹配如 `Bash(npm test:*)` 只放行 `npm test` 前缀。多源合并：`PERMISSION_RULE_SOURCES = [userSettings, projectSettings, localSettings, flagSettings, policySettings, 'cliArg', 'command', 'session']`。`persistPermissionUpdates` 写 settings.json。
- **MokioClaw 现状**（`src/mokioclaw/security/approval.py:28-57`）：`RISK_PATTERNS` 正则列表，只有"匹配→需审批"一种行为。无三态、无 ruleContent、无多源合并、无持久化。`agent_mode.py` 的 `check_tool_permission` 硬编码模式门禁。
- **差距类型**：缺失（有审批，无规则系统）
- **对齐价值**：高 — "策略模式"+"多源配置合并"
- **对齐难度**：中 — 新建 `security/rules.py`，`PermissionRule` 数据结构 + `match_rule(tool_name, args, rules)`
- **建议**：定义 `PermissionRule(source, behavior, tool_name, rule_content)`，`load_permission_rules(workspace)` 从 `.mokioclaw/permissions.json` 加载；`check_permission(rules, tool_name, args)` 返回 `allow/deny/ask`；`tool_gate.py` 先查规则再回退 `agent_mode`

### GAP-9: 拒绝追踪与升级机制（DenialTracking）
- **Claude Code 机制**（第 13 章 13.5，行 4676-4717）：`DENIAL_LIMITS = {maxConsecutive: 3, maxTotal: 20}`。连续拒 3 次或累计 20 次 → `shouldFallbackToPrompting` 触发停止自动审批。`recordDenial`/`recordSuccess` 纯函数返回新状态。子代理独立 `localDenialTracking`。
- **MokioClaw 现状**：完全无拒绝追踪。`classify_tool_call` 每次独立判定不记录历史。`tool_gate.py` 的 `gate_tool_call` 无拒绝计数。
- **差距类型**：缺失
- **对齐价值**：高 — "失败关闭"+"防模型暴力重试"
- **对齐难度**：**低** — `RuntimeState` 加 `denial_tracking: dict`，`tool_gate.py` 记录连续拒绝数
- **建议**：`RuntimeState` 加 `consecutive_denials: int` 和 `total_denials: int`；`gate_tool_call` 返回 deny 时递增；达阈值返回"too many denials, switch to manual"

---

## 中价值组（11 项）

### GAP-10: 工具接口的 `buildTool` 工厂 + `TOOL_DEFAULTS` 失败关闭默认值
- **机制**（第 7 章 7.2，行 2199-2267）：`TOOL_DEFAULTS` 失败关闭默认值：`isConcurrencySafe: () => false`（默认独占）、`isReadOnly: () => false`（默认视为写）、`isDestructive: () => false`（防告警疲劳）、`checkPermissions: () => {behavior:'allow'}`。`buildTool(def)` 用 `{...TOOL_DEFAULTS, ...def}` 覆盖。`DefaultableToolKeys` 明确可省略方法集。
- **现状**（`registry.py:55-149`）：`build_tools` 直接 `StructuredTool.from_function` 构建，无工厂、无默认值、无可省略集。并发安全通过独立 `TOOL_CONCURRENCY_META` 字典管理，与工具定义分离。
- **类型**：缺失 · **价值**：中 · **难度**：中
- **建议**：定义 `@dataclass class ToolDef`（name, func, description, is_concurrency_safe=False, is_read_only=False, max_result_chars=50000）；`build_tool(def)` 应用默认值

### GAP-11: `ToolUseContext` 依赖注入载体（40+ 字段显式参数对象）
- **机制**（第 7 章 7.3，行 2269-2335）：`ToolUseContext` 显式参数对象，40+ 字段分 6 层（配置/状态/UI/追踪/子代理/内容）。`setAppState`（隔离通道：子代理状态不泄漏父级）vs `setAppStateForTasks`（共享通道：穿透根 store 注册全局基础设施）。
- **现状**（`core/utils.py:398-513`）：`execute_tool_by_name` 接收分散参数 `tools, call, hook_runner, budget, workspace, runtime`。`RuntimeState` 承担部分 context 角色，无独立 `ToolUseContext`。子代理 `_spawn_child_runtime` 复制 runtime，共享 `hook_runner`/`result_budget`，无双通道。
- **类型**：部分 · **价值**：中 · **难度**：中
- **建议**：`@dataclass class ToolUseContext`（runtime, hook_runner, budget, workspace, agent_id, abort_controller）；`execute_tool_by_name(ctx, call)`；子代理 `create_subagent_context(parent)`

### GAP-12: Fork 子代理的 prompt cache 优化（`buildForkedMessages` 占位符）
- **机制**（第 11 章 11.3，行 3727-3757）：Fork `model: 'inherit'`（必须，API cache key 含 model id）。`buildForkedMessages` 克隆父级 assistant 消息，构建占位 `tool_result` 用常量 `FORK_PLACEHOLDER_RESULT = 'Fork started — processing in background'`（所有 fork 相同），只有最后的 `directive` 文本块唯一。第一个 fork 创建 cache，后续命中。
- **现状**（`agent_tool.py:62-131`）：`fork_subagent` 用 `model='inherit'` 但实际调 `create_model()`（从 env 读，非显式继承父实例）。`_build_child_system` 复制父级前缀+动态块，无占位 tool_result 优化，每个子代理从头构建。无 `FORK_PLACEHOLDER_RESULT`。
- **类型**：缺失 · **价值**：中 · **难度**：中
- **建议**：`fork_subagent` 构建占位 tool_result（常量文本）+ 克隆父级 assistant blocks + 末尾唯一 directive

### GAP-13: 会话恢复的三道消息清洗过滤器
- **机制**（第 11 章 11.2.4，行 3849-3863）：Resume 消息按序过三道：`filterUnresolvedToolUses`（移除无配对 tool_result 的 tool_use）、`filterOrphanedThinkingOnlyMessages`（移除中断轮纯 thinking）、`filterWhitespaceOnlyAssistantMessages`（移除空白 assistant）。
- **现状**（`token_budget.py:226-276`）：`filter_unresolved_tool_uses` 已实现（对齐 1/3）。后两道缺失。`checkpoint.py:379-383` 的 `deserialize_messages` 只做转换无清洗。
- **类型**：部分（对齐 1/3） · **价值**：中 · **难度**：**低**
- **建议**：新建 `filter_orphaned_thinking_only(messages)` 和 `filter_whitespace_only_assistant(messages)`，`load_strict_state` 反序列化后调用

### GAP-14: `compact_boundary` 标记 + 消息按 API 轮次分组
- **机制**（第 18 章 18.4，行 6095-6145）：`SystemCompactBoundaryMessage` 标记压缩边界，含 `preservedSegment: {anchorUuid, headUuid, tailUuid}` 重建。`groupMessagesByApiRound` 按每次 assistant 响应的 `message.id` 分组，确保压缩在安全边界切割。摘要九段式 prompt（Primary Request / Key Technical Concepts / Files / Errors / Problem Solving / User Messages / Pending Tasks / Current Work / Next Step），输出 `<analysis>`（草稿）+ `<summary>` 标签。
- **现状**（`nodes.py:682-835`）：`context_compressor_node` 用 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 全量替换，无 boundary 标记、无 preservedSegment、无 API 轮次分组。摘要 `CONTEXT_COMPRESSION_PROMPT`（非九段式），无 `<analysis>`/`<summary>` 标签。
- **类型**：部分 · **价值**：中 · **难度**：中
- **建议**：定义 `CompactBoundaryMessage`（`SystemMessage` 子类带 `is_compact_boundary=True`）；`group_messages_by_api_round(messages)` 按 `AIMessage` 边界分组；改 prompt 为九段式

### GAP-15: 消息归一化管线（`normalizeMessagesForAPI`）
- **机制**（第 5 章 5.5，行 1684-1731）：API 调用前五阶段归一化：过滤 virtual/progress 消息 → 附件重排序（tool_result 之上的 attachment 移到前面）→ 连续 user 消息合并（Bedrock 不支持）→ `local_command` system 转 user（API 拒绝 mid-conversation system role）→ 错误媒体剥离。
- **现状**：完全缺失。LangChain `model.invoke(messages)` 直接发送原始消息列表，无归一化。LangGraph `add_messages` 会合并同 id 消息但不做 API 前归一化。
- **类型**：缺失 · **价值**：中 · **难度**：**低**
- **建议**：新建 `memory/normalize.py`，`normalize_for_api(messages)` 五阶段处理；invoke 前调用

### GAP-16: Agent 模型定义（Markdown frontmatter 三源合一 + 优先级覆盖）
- **机制**（第 10 章 10.1-10.3，行 3306-3370）：三源合一：`BuiltInAgentDefinition`/`CustomAgentDefinition`/`PluginAgentDefinition` 共享 `BaseAgentDefinition`。`source` 字段优先级：built-in → plugin → user → project → managed。Markdown frontmatter"宽容输入，严格输出"。5 个内置 Agent 各有不同 `model`/`tools`/`omitClaudeMd`。
- **现状**（`agents/code_agent.py`、`agents/search_agent.py`）：Agent 是**硬编码 Python 函数**，无 frontmatter、无类型系统、无优先级覆盖、无 `source` 字段。`prompts/agent_prompt.py` 是字符串常量。无 `omitClaudeMd`。无 Explore/Plan/Verification 等专业化 Agent。
- **类型**：缺失 · **价值**：中 · **难度**：高
- **建议**：`@dataclass class AgentDefinition`（agent_type, source, model, tools, disallowed_tools, system_prompt, omit_claude_md）；从 `.mokioclaw/agents/*.md` frontmatter 加载；`get_active_agents()` 按优先级合并

### GAP-17: `resolveAgentTools` 两层工具过滤
- **机制**（第 10 章 10.4，行 3538-3591）：第一层 `filterToolsForAgent`：MCP 工具恒通过、全局禁用集、自定义 Agent 禁用集、异步 Agent 白名单。第二层 `resolveAgentTools`：应用 `disallowedTools` 黑名单、`tools: ['*']` 通配展开、`Agent(worker,researcher)` 语法限制可递归 agent 类型。
- **现状**（`agent_tool.py:195-218`）：`_filter_child_tools` 用 `_DEFAULT_SUBAGENT_TOOLS` 集合 + 深度限制收回 `AgentTool`。无全局禁用集、无 MCP 恒通过、无 `tools: ['*']` 通配、无 `Agent(type1,type2)` 递归类型限制。
- **类型**：部分 · **价值**：中 · **难度**：中
- **建议**：第一层 `filter_tools_for_agent(tools, agent_def)` 应用 `disallowed_tools`；第二层 `resolve_agent_tools(tools, allowed)` 展开 `'*'`

### GAP-18: 工具列表排序保 prompt cache breakpoint
- **机制**（第 7 章 7.4，行 2369-2387 + 第 21 章 21.4，行 6968-6996）：工具列表排序"内置在前、MCP 在后"，确保 cache breakpoint 落在最后一个内置工具后。`CacheSafeParams` 确保 fork 子代理用**完全相同**的 system prompt + tool set 命中父级 cache。`promptCacheBreakDetection` 检测 cache 命中率异常。compact 后 `notifyCompaction` 重置 cache 基线。
- **现状**（`prompts/builder.py:74-100`）：`PromptBuilder` 有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分界线（已对齐），但无 `CacheSafeParams`、无 cache 命中率检测、无 compact 后基线重置。工具列表排序在 `registry.py:build_tools` 是**代码顺序**（非内置在前/MCP 在后分离排序）。
- **类型**：部分 · **价值**：中 · **难度**：**低**
- **建议**：`build_tools` 返回时把内置工具和 MCP 工具分别排序后拼接；`fork_subagent` 确保子代理用父级相同 static 前缀

### GAP-19: 后台任务 Resume 机制（`resumeAgentBackground`）
- **机制**（第 11 章 11.2，行 3799-3833）：`resumeAgentBackground` 加载持久化 transcript + metadata → 三道消息清洗 → 重建 `contentReplacementState` → 检查 worktree 是否存在 → 识别 agent 类型 → 注册为后台任务 → 在 `runWithAgentContext` 中运行。
- **现状**（`reliability/background_tasks.py`）：`BackgroundTaskRegistry` 纯内存表（`_tasks: dict`），进程退出即丢。`checkpoint.py` 的 `load_resume_inputs` 恢复**主工作流状态**非后台子代理任务。子代理无法跨会话恢复。
- **类型**：缺失 · **价值**：中 · **难度**：高（依赖 GAP-5 先到位）
- **建议**：后台任务完成时把 `final_report` 写入 `.mokioclaw/background/{task_id}.json`；`--resume` 加载未完成任务状态

### GAP-20: Skill 系统的 Fork 执行模式 + 参数替换 + Shell 模板
- **机制**（第 12 章 12.2-12.3，行 4081-4307）：两种执行模式：`inline`（注入当前对话）和 `fork`（启动独立子代理）。`context: 'fork'` 时 `executeForkedSkill` 构建 agent def → 隔离 AppState → 运行 `runAgent`。参数替换 `$ARGUMENTS`/`$ARG1`。Shell 模板 `{{ ls -la }}` 加载时执行嵌入输出。Bundled Skill 含 `files: Record<string, string>` 携带参考文件，首次调用提取到磁盘。Skill 发现预算 `SKILL_BUDGET_CONTEXT_PERCENT=0.01`。
- **现状**（`tools/skill.py`、`interaction/commands.py:106-118`）：Skill 只有 inline 模式（`load_skill_markdown` 读正文注入消息）。无 fork 执行、无参数替换、无 shell 模板、无 bundled files、无发现预算管理。`SkillTool`（`registry.py:200-234`）只返回正文文本。
- **类型**：部分 · **价值**：中 · **难度**：中（fork 模式依赖子代理系统；参数替换较简单）
- **建议**：`dispatch_slash_command` 加 `$ARGUMENTS` 替换；Skill frontmatter 加 `context: fork`，fork 模式调 `fork_subagent`

---

## 低价值组（8 项 — 语言/框架差异，建议不对齐）

### GAP-21: `feature()` 编译时死代码消除
- **机制**（第 2 章 2.3，行 403-455；第 24 章 24.8，行 7778-7828）：`feature('FLAG')` 在 Bun bundler 阶段求值为编译时常量，false 分支 `require()` 整个被 DCE，外部 binary 物理上不含内部功能代码。
- **现状**：Python 动态语言无编译时 DCE，用 `os.getenv` 做运行时条件分支。
- **类型**：偏差（语言差异不可对齐）· **价值**：低 · **难度**：高（需改变语言）
- **建议**：不对齐；文档注明此为 TypeScript/Bun 特有优化

### GAP-22: `createStore` 工厂 + `useSyncExternalStore` 响应式订阅
- **机制**（第 17 章 17.1，行 5633-5687）：34 行 Zustand 风格 store：`createStore(initialState, onChange)` 返回 `{getState, setState, subscribe}`。`setState` 函数式更新器防并发丢失。`useSyncExternalStore` 集成 React。`onChangeAppState` 集中处理副作用（"单一咽喉点"）。
- **现状**：`RuntimeState` 普通 `@dataclass`，无订阅/响应式。`MokioGraphState` 是 `TypedDict` 由 LangGraph 管理。事件通过 `EventBus`（`core/events.py`）推送，非状态订阅。
- **类型**：偏差（LangGraph 管状态）· **价值**：低 · **难度**：高
- **建议**：不对齐；用 EventBus 做事件订阅

### GAP-23: `FileStateCache` LRU 双维度驱逐 + 路径归一化
- **机制**（第 17 章 17.5，行 5820-5884）：`FileStateCache` 包装 `LRUCache`，`max: 100` 条 + `maxSize: 25MB`。`sizeCalculation` 用 `Buffer.byteLength(content)`。所有 key 过 `normalize()` 归一化（`/a/b/../c` → `/a/c`）。`isPartialView` 标记强制 Edit/Write 重新 Read。`mergeFileStateCaches` 按时间戳合并。
- **现状**（`state/runtime.py:72,154-220`）：`read_files: dict[Path, FileSnapshot]` 普通字典，无 LRU 驱逐、无大小限制。`record_read` 用 `path.resolve()` 归一化但不处理 `..`。无 `isPartialView`。无跨会话合并。
- **类型**：部分 · **价值**：低（教学项目文件少）· **难度**：低
- **建议**：如需对齐，`read_files` 改 `OrderedDict` 加 `maxlen=100`；`record_read` 中 `path = path.resolve()` 前加 `os.path.normpath`

### GAP-24: React + Ink TUI 渲染管线
- **机制**（第 19 章，行 6265-6550）：vendor 完整 Ink 实现，ConcurrentRoot + 30 FPS 帧循环 + Yoga WASM Flexbox + ANSI 解析器 + 对象池 + 差异化输出。
- **现状**（`interaction/tui/app.py`）：用 Textual 框架，非自研渲染管线。
- **类型**：偏差（框架选择不同）· **价值**：低 · **难度**：高（不现实）
- **建议**：不对齐

### GAP-25: `memoizeWithTTL` / `memoizeWithLRU` 三层缓存
- **机制**（第 21 章 21.2，行 6876-6938）：三层：`memoizeWithTTL`（过期返旧值+后台刷新）、`memoizeWithTTLAsync`（in-flight 去重）、`memoizeWithLRU`（有界 LRU，替代 lodash 无界 memoize 的 300MB 泄漏）。
- **现状**（`providers/openai_provider.py:26-49`）：`_validated_env` 模块级缓存（简单单例），无 TTL/LRU/去重。
- **类型**：缺失 · **价值**：低（教学项目缓存需求有限）· **难度**：低
- **建议**：如需对齐，关键路径（MCP 工具列表、token 估算）加 `@lru_cache(maxsize=100)`

### GAP-26: MCP OAuth 认证体系
- **机制**（第 16 章，行 5309-5624）：`ClaudeAuthProvider` 实现 OAuth 全生命周期，含 token 刷新锁、CIMD、三步授权服务器发现、安全存储、XAA 跨账户访问。
- **现状**（`mcp/transport.py`）：`HttpSSETransport` 只支持 `headers` 传入，无 OAuth 流程。
- **类型**：缺失 · **价值**：低（教学项目 MCP 服务器通常无需 OAuth）· **难度**：高
- **建议**：不对齐；文档注明企业级特性

### GAP-27: Speculative Classifier Check（竞赛设计）
- **机制**（第 13 章 13.4，行 4628-4654）：`pendingClassifierCheck` 是一个 Promise，用户看到权限对话框前并行启动分类器。分类器先返回"安全"则对话框永不出现。
- **现状**（`security/classifier.py:101-136`）：`classify_tool_call` 同步调用，无 speculative 预启动。
- **类型**：缺失 · **价值**：低（需异步 LLM 分类器，教学复杂度过高）· **难度**：高
- **建议**：不对齐

### GAP-28: 迁移系统（跨版本数据兼容）
- **机制**（第 23 章 23.4，行 7520-7550）：`CURRENT_MIGRATION_VERSION = 11`，启动时 `runMigrations()` 检查版本执行同步迁移，每个迁移函数幂等。
- **现状**：无迁移系统。`config/loader.py` 直接读配置无版本检查。
- **类型**：缺失 · **价值**：低（教学项目版本少）· **难度**：低
- **建议**：如需对齐，`config/loader.py` 加 `MIGRATION_VERSION` 检查

---

## 建议的对齐优先级排序 Top 10

排序逻辑：先扫"高价值+低难度"（GAP-3/9 打头），再"中价值+低难度"快赢（GAP-13/15/18），然后攻坚"高价值+中难度"（GAP-1/7/8/4）。GAP-2/5 高难度高价值，留作深度对齐，且受 LangGraph 约束需做教学版简化。

| # | Gap ID | 标题 | 价值 | 难度 |
|---|---|---|---|---|
| 1 | GAP-3 | 每工具 `maxResultSizeChars` 阈值 + 三层优先级 | 高 | **低** |
| 2 | GAP-9 | DenialTracking 拒绝追踪升级 | 高 | **低** |
| 3 | GAP-13 | 会话恢复补两道清洗（orphaned/whitespace） | 中 | **低** |
| 4 | GAP-15 | `normalizeMessagesForAPI` 五阶段归一化 | 中 | **低** |
| 5 | GAP-18 | 工具列表排序保 cache breakpoint | 中 | **低** |
| 6 | GAP-1 | `isConcurrencySafe` 输入驱动判定 | 高 | 中 |
| 7 | GAP-7 | Bash 语义分析器（引号/命令替换） | 高 | 中 |
| 8 | GAP-8 | 权限规则系统（多源合并+ruleContent） | 高 | 中 |
| 9 | GAP-4 | ContentReplacementState 全局预算 | 高 | 中 |
| 10 | GAP-6 | 大输出落盘标准化消息格式 | 中 | **低** |

---

## 补充说明

### MokioClaw 已对齐得很好的部分
引擎层恢复链（BudgetTracker/OutputTokenRecovery/PromptTooLongRecovery/filter_unresolved_tool_uses/invoke_with_fallback/model_with_max_tokens）· Hook 系统（7 种事件+command/http/prompt 三种 type）· 动静分离 PromptBuilder · MCP 渐进披露+resources 目录 · handoff 分类器 · ToolSearch 延迟加载 · microcompact+snip 两层规则压缩 · AutoMemory+AutoDream · contextModifier（cd/env 注入）· 后台任务注册表 · checkpoint light/strict 双模式。这些不需要再动。

### 最大的架构偏差
MokioClaw 用 LangGraph 的 `StateGraph` 管理工作流状态，Claude Code 用自研 `QueryEngine` 状态机。这导致 GAP-2（StreamingToolExecutor）和 GAP-5（Transcript）难以直接对齐 —— LangGraph 的 `add_messages` reducer 和 `stream_mode` 已提供部分能力，但也限制自定义调度器空间。建议对齐 GAP-2 时以"教学版简化 TrackedToolExecutor"形式实现，不试图完全复制 Claude Code 的 AsyncGenerator 管线。
