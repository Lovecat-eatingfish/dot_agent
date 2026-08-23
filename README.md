<p align="center">
  <img src="assets/logo.png" alt="dot_agent Logo" width="460" />
</p>

<h1 align="center">dot_agent</h1>

<p align="center">
  从零开始搭建的多智能体 CodeAgent 系统，基于 LangGraph 实现 Agent 规划、工具调用、上下文压缩与结果验证的完整闭环。
</p>

---

## 项目概述

dot_agent 是从零开始自主开发的多智能体 CodeAgent 系统，完整实现了 Agent 规划、工具调用、上下文压缩、多轮记忆与结果验证的工程闭环。

项目的目标是构建一个真正可运行的 Agent 系统：从任务理解、计划制定、子 Agent 委派、工具执行到结果验证，每个环节都有清晰的实现和可控的行为边界。

## 核心特性

### 多智能体协作

基于 LangGraph StateGraph 构建 planner → coding_agent → verifier 的协作流水线：

- **plan_node**：分析任务、生成计划与 todo 列表，通过 JSON 路由决策委派给专业 Agent
- **coding_agent**：读写文件、执行命令、更新 todo 进度、运行检查
- **valid_node**：只读检查 workspace 文件与命令结果，判定任务是否完成
- **replan 循环**：verifier 失败时生成修复建议，路由到 repair 节点委派 coding_agent 修复，再回到 verifier 校验

### 上下文工程

- **双阈值压缩策略**：`soft_threshold=70%` 触发增量压缩（保留 20 条消息 + 叠加旧摘要），`hard_threshold=90%` 触发全量压缩（清空消息 + 保留 10 条 + 新摘要）
- **分级消息处理**：按消息重要性分为四级 — KEEP_ALWAYS → COMPRESS_LIGHTLY → COMPRESS_HEAVILY → DROP
- **持久化历史**：压缩前将完整消息追加写入 `RAW_HISTORY.md`，保留审计与重建能力

### 三层记忆系统

| 层级 | 内容 | 生命周期 |
| --- | --- | --- |
| `rules` | 用户自定义规则 | 跨任务持久化 |
| `working_memory` | 当前任务的关键信息 | 单任务内 |
| `history_summary_store` | 历史对话压缩摘要 | 跨轮次 |

支持记忆索引化与渐进式披露——启动时仅加载索引（~200 tokens），按需读取完整文件。

### 提示词动静分离

参考 Claude Code memory 文件模式，将提示词拆分为三层：

- **静态层**：`prompts/agent_prompt.py` 中的角色定义与规则模板
- **动态层**：`~/.mokioclaw/CLAUDE.md` + `.mokioclaw/config.md`（YAML frontmatter + markdown body），用户自定义指令自动注入所有 Agent 的 system prompt 末尾
- **运行时层**：task / plan / memory 由各节点通过 HumanMessage 在调用时注入

### 可靠性与工程化

- **检查点系统**：轻量模式保存状态摘要，严格模式保存完整 graph state；支持版本化快照枚举与 rollback 回退
- **钩子系统**：EventBus 实现 publish/subscribe 模式，内置 TUI 推送、CLI 打印、checkpoint 触发、trace 写入四个开箱即用的事件处理器
- **并行工具调度**：自动检测工具间文件级依赖，独立调用并行执行，TodoUpdate 强制串行避免竞态
- **安全机制**：路径遍历防护、高风险命令人工审批（inline/auto/deny 三模式）
- **双交互模式**：Rich CLI（单次任务事件时间线）与 Textual TUI（多轮对话全屏界面）

## 快速开始

### 环境配置

```bash
# 安装依赖
uv sync

# 配置 .env
cp .env_example .env
# 编辑 .env 填入 API_KEY / MODEL / BASE_URL / TAVILY_API_KEY
```

### 运行

```bash
# 单次任务（Rich CLI）
uv run dotagent "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"

# 多轮对话（Textual TUI）
uv run dotagent tui

# 恢复中断的任务
uv run dotagent --resume .mokioclaw/workspaces/workspace-YYYYMMDD-HHMMSS-xxxxxx
```

### 测试

```bash
uv run pytest -q
```

## 用户自定义配置

dot_agent 支持两级用户配置，采用 YAML frontmatter + markdown body 格式：

```markdown
<!-- ~/.mokioclaw/CLAUDE.md 或 .mokioclaw/config.md -->
---
approval_mode: inline
max_attempts: 5
bash_timeout: 300
---

# Custom Instructions

以下指令会注入到所有 agent 的 system prompt 中：

- Always add type hints to Python code
- Use async/await for I/O operations
- Write tests for every new function
```

项目级配置覆盖全局配置的相同字段，markdown body 追加到 agent prompt 末尾。

详细说明见 [`.mokioclaw/config.md.example`](.mokioclaw/config.md.example)。

## 项目结构

```text
src/dot/
├─ core/                # 基础设施：日志 / LLM / hooks / 审批 / 路径安全 / 预算 / 运行时
├─ tools/               # 基础工具：file / bash / glob / grep + 渐进披露元工具
├─ mcp/                 # MCP 协议栈 + 渐进披露
├─ skills/              # Skill 发现与渐进披露
├─ session/             # 会话域：Session / 持久化 / SessionManager
├─ graph/               # LangGraph 图编排 + 节点提示词
├─ compress/            # 上下文压缩：budget / l1_extract / l2_summarize
├─ trace/               # 链路追踪：Tracer / exporter
├─ host/                # Agent 统一入口：AgentHost / SharedServices
├─ hook/                # Hook 机制
├─ constant/            # 常量定义
└─ main.py              # 控制台入口
```

## 技术栈

| 层面 | 选型 |
| --- | --- |
| LLM 编排 | LangChain + LangGraph |
| CLI 框架 | Typer + Rich |
| TUI 框架 | Textual |
| Web 搜索 | Tavily |
| 包管理 | uv + Hatchling |
| 测试 | pytest |

Python >= 3.13

## 文档

- [项目全景讲解](docs/project-overview.md)
- [实现完善报告](docs/IMPLEMENTATION_SUMMARY.md)
- [简历描述](docs/resume.md)
- [记忆系统快速参考](docs/MEMORY_QUICK_REFERENCE.md)
- [架构设计文档](doc/architecture-design.md)
- [链路追踪设计说明](doc/链路追踪设计说明.md)
- [权限控制设计](doc/fix-权限控制.md)
