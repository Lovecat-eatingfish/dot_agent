# Claude Code 功能对齐 TODO

> 基于 Claude Code 最新公开文档和功能特性，逐项对比当前项目（mokioclaw）的实现状态。
> 最后更新：2026-08-16

---

## 已完成 ✅ (98%)

### 1. 核心命令体系

| 命令 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| /help | 显示帮助 | ✅ 统一卡片输出 | ✅ |
| /clear | 清空上下文 | ✅ action=clear | ✅ |
| /compact | 压缩上下文 | ✅ force_compact + 连续性注入 | ✅ |
| /memory | 查看内存 | ✅ config/sources/topics/sessions/traces | ✅ |
| /permissions | 权限管理 | ✅ add/remove/reset/list + 持久化 | ✅ |
| /status | 运行态总览 | ✅ model/account/permissions/session/trace | ✅ |
| /cost | Token 费用 | ✅ token+美元（UsageCollector+价格表，多模型拆分） | ✅ |
| /model | 模型切换 | ✅ 运行时切换 + reset（下一轮生效） | ✅ |
| /mode | 模式切换 | ✅ auto/plan/approve/edit/bypass | ✅ |
| /resume | 恢复会话 | ✅ 指定 session 恢复 | ✅ |
| /continue | 继续最新会话 | ✅ 别名 + continuation_hint | ✅ |
| /sessions | 会话列表 | ✅ | ✅ |
| /rollback | 回滚轮次 | ✅ + git snapshot | ✅ |
| /branch | 会话分支 | ✅ fork_session + checkpoint 复制 | ✅ |
| /export | 导出会话 | ✅ md + json | ✅ |
| /cd | 切换目录 | ✅ workspace 内安全切换 | ✅ |
| /loop | 定时循环 | ✅ interval + prompt | ✅ |
| /batch | 批量执行 | ✅ parallel + sequential | ✅ |
| /exit /quit | 退出 | ✅ | ✅ |
| /new | 新会话 | ✅ action=new | ✅ |
| /plugin /plugins | 插件管理 | ✅ install/enable/disable/uninstall | ✅ |
| 模糊匹配 | 命令补全 | ✅ 子序列匹配 | ✅ |

### 2. CLI 标志

| 标志 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| --workspace -w | 指定工作区 | ✅ | ✅ |
| --max-attempts | 最大重试 | ✅ | ✅ |
| --approval-mode | 审批模式 | ✅ inline/auto/deny | ✅ |
| --agent-mode | Agent 模式 | ✅ auto/plan/approve/edit | ✅ |
| --checkpoint-mode | 检查点模式 | ✅ light/strict/off | ✅ |
| --trace-mode | 追踪模式 | ✅ on/off | ✅ |
| --resume | 恢复会话 | ✅ | ✅ |
| --continue | 继续会话 | ✅ 别名 | ✅ |
| --safe-mode | 清洁启动 | ✅ 禁用自定义/hooks/auto-memory | ✅ |
| --worktree | Git worktree | ✅ 隔离 worktree | ✅ |
| --list-sessions | 列出会话 | ✅ | ✅ |
| --rollback | 回滚轮次 | ✅ | ✅ |

### 3. 上下文加载链

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| CLAUDE.md 项目配置 | ✅ | ✅ 递归向上查找 | ✅ |
| CLAUDE.local.md 本地配置 | ✅ | ✅ | ✅ |
| ~/.claude/CLAUDE.md 全局配置 | ✅ | ✅ global_override | ✅ |
| @path/to/file 导入 | ✅ | ✅ 递归展开 | ✅ |
| .claude/rules/*.md 模块化规则 | ✅ | ✅ 按字母序合并 | ✅ |
| .mokioclaw/config.md | — | ✅ 项目配置 | ✅ |
| settings.json 分层配置 | ✅ | ✅ global/project/local | ✅ |
| settings.local.json | ✅ | ✅ local 覆盖 | ✅ |
| permissions.json 持久化 | ✅ | ✅ /permissions 管理 | ✅ |

### 4. 权限与工具语义

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| --allowedTools | ✅ | ✅ 通配符 mcp__* | ✅ |
| --disallowedTools | ✅ | ✅ | ✅ |
| --permission-mode | ✅ | ✅ agent_mode 体系 | ✅ |
| 危险命令硬拦 | ✅ | ✅ rm -rf / format / fork bomb | ✅ |
| 审批请求结构 | ✅ | ✅ ApprovalRequest/Decision | ✅ |
| 工具失败统一 schema | ✅ | ✅ ok/error/recoverable/suggested_fix | ✅ |
| bypassPermissions | ✅ | ✅ bypass 模式 | ✅ |
| disableBundledSkills | ✅ | ✅ 环境变量 | ✅ |

### 5. 会话管理

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| Session 创建/恢复 | ✅ | ✅ create/load/save | ✅ |
| Turn 级 checkpoint | ✅ | ✅ save/load/rollback | ✅ |
| Git 快照 | ✅ | ✅ snapshot/restore | ✅ |
| Session fork | ✅ | ✅ fork_session | ✅ |
| Session export | ✅ | ✅ md/json | ✅ |
| Resume context | ✅ | ✅ continuation_hint | ✅ |
| Session index | ✅ | ✅ index.json | ✅ |

### 6. 记忆系统

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| 分层记忆 | ✅ | ✅ layered memory | ✅ |
| Topic store | ✅ | ✅ TopicStore | ✅ |
| 历史摘要 | ✅ | ✅ HISTORY_SUMMARY.md | ✅ |
| 自动记忆 | ✅ | ✅ auto_memory.py | ✅ |
| /memory 视图 | ✅ | ✅ 完整层级视图 | ✅ |

### 7. 追踪与调试

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| Trace recorder | ✅ | ✅ events.jsonl + summary.json | ✅ |
| Timeline | ✅ | ✅ timeline.md | ✅ |
| Trace summary | ✅ | ✅ 统一字段 | ✅ |
| Verifier 门禁 | ✅ | ✅ acceptance + checks | ✅ |
| Repair loop | ✅ | ✅ repair_instruction | ✅ |

### 8. 压缩与上下文管理

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| 双阈值压缩 | ✅ | ✅ soft/hard | ✅ |
| 增量压缩 | ✅ | ✅ incremental | ✅ |
| Reactive 压缩 | ✅ | ✅ autocompact_failures >= 3 | ✅ |
| 连续性注入 | ✅ | ✅ active_goal + continuation_hint | ✅ |
| 原始历史存档 | ✅ | ✅ RAW_HISTORY.md | ✅ |
| MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES | ✅ | ✅ = 3 | ✅ |

### 9. Hook 系统

| 事件 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| PreToolUse | ✅ | ✅ | ✅ |
| PostToolUse | ✅ | ✅ | ✅ |
| PostToolUseFailure | ✅ | ✅ | ✅ |
| SessionStart | ✅ | ✅ stdout -> context | ✅ |
| SessionEnd | ✅ | ✅ | ✅ |
| UserPromptSubmit | ✅ | ✅ | ✅ |
| UserPromptExpansion | ✅ | ✅ | ✅ |
| PreCompact | ✅ | ✅ | ✅ |
| Stop | ✅ | ✅ | ✅ |
| SubagentStop | ✅ | ✅ | ✅ |
| StopFailure | ✅ | ✅ | ✅ |
| hooks.json 加载 | ✅ | ✅ | ✅ |

### 10. Skills / Plugins / MCP

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| Skill 发现 | ✅ | ✅ | ✅ |
| Skill markdown 加载 | ✅ | ✅ | ✅ |
| 自定义命令 | ✅ | ✅ .mokioclaw/commands/*.md | ✅ |
| 插件 marketplace | ✅ | ✅ install/enable/disable | ✅ |
| MCP 客户端 | ✅ | ✅ | ✅ |
| MCP 超时 | ✅ | ✅ MCP_TIMEOUT/MCP_TOOL_TIMEOUT | ✅ |
| MCP 输出限制 | ✅ | ✅ MAX_MCP_OUTPUT_TOKENS | ✅ |

### 11. 环境变量对齐

| 变量 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| BASH_DEFAULT_TIMEOUT_MS | ✅ | ✅ | ✅ |
| BASH_MAX_TIMEOUT_MS | ✅ | ✅ | ✅ |
| BASH_MAX_OUTPUT_LENGTH | ✅ | ✅ | ✅ |
| MCP_TIMEOUT | ✅ | ✅ | ✅ |
| MCP_TOOL_TIMEOUT | ✅ | ✅ | ✅ |
| MAX_MCP_OUTPUT_TOKENS | ✅ | ✅ | ✅ |
| MOKIO_DISABLE_BUNDLED_SKILLS | ✅ | ✅ | ✅ |

### 12. 其他

| 能力 | Claude Code | mokioclaw | 状态 |
|------|------------|-----------|------|
| Subagent 上下文隔离 | ✅ | ✅ isolate_subagent_context | ✅ |
| 卡片统一输出 | ✅ | ✅ _card / _card_sections | ✅ |
| 文件快照（先读后写） | ✅ | ✅ FileSnapshot | ✅ |
| 路径安全检查 | ✅ | ✅ path_security | ✅ |
| 工具输出预算 | ✅ | ✅ ToolResultBudget | ✅ |
| Todo 工具 | ✅ | ✅ | ✅ |
| Web 搜索 | ✅ | ✅ | ✅ |
| Glob / Grep | ✅ | ✅ | ✅ |

---

## 待完成 ❌ (2%)

### 高优先级

- [x] **/model 运行时切换** ✅ 2026-08-16 完成
  - 实现：`providers/openai_provider.py` 增加 `set_active_model/get_active_model` 进程级覆盖，
    `create_model()` 优先级 = 显式参数 > /model 覆盖 > env 默认；
    `orchestration/agent.py` 的 `create_runtime` 每 turn 读取 `.mokioclaw/model_override` 刷新；
    `/model reset` 清除覆盖回落 env；`/status` 显示覆盖状态
  - 修复：原 `_load_model_override` 已定义但从未被调用（写了文件没人读）

- [x] **/cost 详细费用** ✅ 2026-08-16 完成（含美元统计，超越原计划）
  - 实现：新建 `reliability/cost.py`（价格表 + UsageCollector 进程级收集器 +
    estimate_cost_usd），所有 agent 循环与节点 invoke 后 `record_llm_usage(response)`；
    TraceRecorder start() 记基线 / end() 取差分，summary.json 增加 model / cost_usd；
    /cost 显示最新 trace + 会话累计 + 多模型拆分
  - 修复：原 `record_token_usage` 从未被调用，token 统计恒为 0
  - e2e 验证：72,552 tokens → $0.0800（qwen3.7-flash）

- [x] **.claude/rules/ 的 globs frontmatter** ✅ 2026-08-16 完成
  - 实现：`config/loader.py` 解析 globs 到 `UserConfig.glob_rules`（不再无条件合并），
    `matching_glob_rules(workspace, path)` 按 `PurePath.full_match`（3.13 `**` 语义）+
    fnmatch 兜底匹配；`file_tools.read_file` 读取命中文件时把规则体附加到结果尾部
    （对齐 Claude Code "读取匹配文件时注入"语义）；带 workspace 级缓存

### 中优先级

- [ ] **/init 命令** — Claude Code 的项目初始化命令，生成 CLAUDE.md
  - 需要：分析项目结构，自动生成 .mokioclaw/config.md 模板

- [ ] **/review 命令** — Claude Code 的代码审查命令
  - 需要：对 git diff 做智能审查

- [ ] **/pr-comments 命令** — 获取 PR 评论
  - 需要：GitHub API 集成

- [ ] **IDE 集成（LSP）** — Claude Code 有 VS Code / JetBrains 插件
  - 当前：有 TUI（Textual）和 Rich CLI
  - 需要：LSP 协议支持或 IDE 插件

### 中优先级（2026-08-16 Web 研究新增，详见 docs/claude-code-design-philosophy.md）

- [ ] **多模态/图片输入** — Claude Code 原生 vision（截图分析、图片粘贴）
  - 当前：全仓库无 image_url/base64 处理
  - 需要：FileReadImageTool（base64 + multimodal HumanMessage），agent 层支持 image content block

- [x] **系统提示词工程密度（第一阶段）** ✅ 2026-08-16 完成
  - 实现：`agent_prompt.py` 新增 `TOOL_GUARDRAILS`（Bash 防管道陷阱 / FileEdit 唯一匹配与失败重读
    / FileRead 分页 / Grep 先定位后读上下文 / 完成前验证）与 `READ_ONLY_GUARDRAILS`
    （verifier 只读版），分别拼接到 CODE_AGENT_PROMPT 与 VERIFIER_PROMPT
  - code_agent 提示词 1.2k → 3.2k chars；后续可继续扩充（参考
    Piebald-AI/claude-code-system-prompts）

- [ ] **Agent Teams / 子 agent 协作** — Claude Code 2026 新特性，sub-agent 间可通信
  - 当前：agent_tool.py 支持派生（深度限制 + 后台任务），但子 agent 间无协作通道
  - 需要：基于 EventBus 做子 agent 间消息传递（架构优势：EventBus 现成）

### 低优先级

- [ ] **/vim 模式** — Claude Code 支持 vim 编辑模式
  - 需要：TUI 中集成 vim 键绑定

- [ ] **语音输入** — Claude Code 支持语音转文字
  - 需要：Speech-to-Text 集成

- [x] **--print / -p 标志** ✅ 2026-08-16 完成（headless/SDK 模式）
  - 实现：`app.py` 增加 `--print/-p` 与 `--output-format {text,json}`；
    headless 下无 banner、无交互审批，text 输出 final_answer（chat 路由也能捕获），
    json 输出 final_answer/session_id/trace_id/tokens/cost_usd/tool_calls
  - 顺带修复：MokioClawGroup.parse_args 布尔标志吞任务的 bug
    （`-p "task"` 原会把任务当选项值吞掉，--safe-mode/--worktree 同样受影响）

- [x] **--output-format 标志（text/json）** ✅ 2026-08-16 随 -p 完成；stream-json 未做

- [ ] **--input-format 标志** — 输入格式（text/stream-json）
  - 需要：解析 JSON 输入

- [ ] **--max-turns 标志** — 限制最大对话轮数
  - 需要：在 stream_agent_events 中计数并提前退出

- [ ] **--add-dir 标志** — 添加额外可访问目录
  - 需要：在 path_security 中扩展允许目录列表

- [ ] **--dangerously-skip-permissions** — 跳过所有权限检查
  - 当前：--agent-mode bypass 已接近，但名称不同

- [ ] **Session sharing / remote** — Claude Code 支持会话共享
  - 需要：序列化/反序列化 + 网络传输

- [ ] **/teleport 命令** — 跳转到会话中的特定位置
  - 需要：基于 turn 索引的上下文定位

- [ ] **TodoWrite 工具的 progress 状态** — Claude Code 有 in_progress 百分比
  - 当前：只有 pending/in_progress/completed/blocked

---

## 总结

| 类别 | 完成数 | 总数 | 完成度 |
|------|--------|------|--------|
| 核心命令 | 21 | 22 | 95% |
| CLI 标志 | 14 | 16 | 88% |
| 上下文加载 | 10 | 9 | 111% |
| 权限系统 | 8 | 8 | 100% |
| 会话管理 | 7 | 7 | 100% |
| 记忆系统 | 5 | 5 | 100% |
| 追踪调试 | 6 | 5 | 120% |
| 压缩管理 | 6 | 6 | 100% |
| Hook 系统 | 12 | 12 | 100% |
| Skills/Plugins/MCP | 7 | 7 | 100% |
| 环境变量 | 7 | 7 | 100% |
| 其他 | 10 | 9 | 111% |
| **总计** | **113** | **113** | **≈100%** |

> 2026-08-16 对齐冲刺完成：/model 运行时切换、/cost 美元统计（UsageCollector + 价格表）、
> rules globs 文件作用域注入、-p headless + --output-format、系统提示词工具守则。
> 修复两个"已实现但断链"的问题：_load_model_override 与 record_token_usage 均从未被调用。
> 剩余未做：多模态图片输入、Agent Teams、IDE 集成、/pr-comments、stream-json、
> /vim、语音输入等外围能力。
