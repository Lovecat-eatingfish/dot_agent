# Claude Code 功能对照与实现清单

> 目的：一份给人学习、也给项目本身当 TODO 的对照文档。
> 说明：这里以 Claude Code 的公开体验和常见工作流为参照，整理成“已具备 / 部分具备 / 待补齐”。

## 1. 总体定位

Claude Code 更像一个“可持续写代码的终端工作台”，不是单纯的聊天机器人。
它的核心是：读项目、理解上下文、修改文件、验证结果、恢复会话、继续工作。

本项目当前已经覆盖了其中一大半骨架，尤其是：
- 会话与恢复
- 检查点与 trace
- 多代理工作流
- 权限与审批
- skills / plugins
- 终端命令入口

但离 Claude Code 的成熟体验，还差在：
- 统一的命令/输出体验
- 更稳定的上下文注入与恢复
- 更完整的权限与安全语义
- 更强的内存加载规则
- 更一致的终态总结

## 2. Claude Code 的核心能力拆解

### 2.1 任务输入

Claude Code 通常接收两类输入：
- 一次性任务：让它做一件具体事
- 持续会话：在同一 workspace 里连续推进

关键特点：
- 支持自然语言任务
- 支持简短续接，比如“继续”“修一下”“跑测试”
- 会自动把任务当成项目工作流，而不是普通闲聊

### 2.2 项目上下文

Claude Code 的项目上下文通常来自：
- 当前 workspace
- 项目根部的 `CLAUDE.md`
- 用户级 `CLAUDE.md`
- 会话历史
- 当前文件、目录、diff、trace

关键特点：
- 支持层级化配置
- 支持自动注入说明
- 支持在长会话中持续维持工作记忆

### 2.3 工具执行

Claude Code 的“能力感”主要来自工具，而不是纯聊天。
常见工具语义包括：
- 读文件
- 写文件
- 编辑文件
- 搜索
- 执行命令
- 调用外部工具 / MCP
- 启动子任务

关键特点：
- 工具调用有明确边界
- 危险操作先解释、先确认
- 输出可追踪、可回放

### 2.4 权限与确认

Claude Code 的权限模型重点不是“能不能做”，而是“做之前要不要确认”。

常见语义：
- 自动允许
- 需要确认
- 仅规划不执行
- 禁止危险命令
- 对特定工具或路径做限制

### 2.5 会话恢复

Claude Code 不是每次都从头开始，而是支持继续上次工作。
这意味着它需要保存：
- 任务摘要
- 最近的计划
- 变更结果
- 失败原因
- 下一步建议

### 2.6 终态输出

Claude Code 的终态输出通常是：
- 任务是否完成
- 改了什么
- 验证了什么
- 还剩什么
- 下一步做什么

它不是大段流水账，而是面向继续工作的摘要。

## 3. 本项目现状

### 3.1 已经具备

#### 会话
- `src/mokioclaw/reliability/session_store.py`
- 支持 session 创建、加载、保存、列出、回滚
- 支持 turn 级检查点
- 支持恢复时生成可注入的 resume context

#### 检查点
- `src/mokioclaw/reliability/checkpoint.py`
- 支持轻量 / 严格 / 关闭模式
- 支持恢复输入加载

#### Trace
- `src/mokioclaw/reliability/trace.py`
- 支持事件记录、汇总、时间线
- 支持终态 summary

#### 权限
- `src/mokioclaw/security/agent_mode.py`
- `src/mokioclaw/security/approval.py`
- `src/mokioclaw/core/tool_gate.py`
- 已有 `auto / plan / approve / edit / bypass`

#### 多代理工作流
- `src/mokioclaw/orchestration/workflow.py`
- `planner -> search_agent / code_agent -> verifier -> repair -> final`

#### skills / plugins
- `src/mokioclaw/tools/skill.py`
- `src/mokioclaw/plugins/loader.py`
- `src/mokioclaw/plugins/marketplace.py`

#### 命令入口
- `src/mokioclaw/interaction/commands.py`
- 已有 `/help`、`/resume`、`/sessions`、`/rollback`、`/mode`、`/memory`、`/compact`、`/cost`、`/model`
- 已开始把输出统一成卡片风格

### 3.2 部分具备

#### CLAUDE.md 风格注入
- 已有项目配置与 prompt builder
- 已有 session / memory 注入
- 但还没有完全统一成 Claude Code 那种“固定层级 + 固定加载顺序”的规则

#### 命令体验
- `/help`、`/resume`、`/status` 已开始统一样式
- 但 `/sessions`、`/rollback`、`/memory`、`/compact` 的展示还没完全收口

#### 恢复体验
- 能恢复 session
- 但恢复时上下文组织还可以更强、更明确、更像“同一条工作线继续下去”

#### 终态输出
- 已有 final summary、trace summary、session summary
- 但叙事结构还在持续统一中

### 3.3 还缺的核心点

#### 更完整的上下文层级
- 用户级 `CLAUDE.md`
- 项目级 `CLAUDE.md`
- 工作区级运行时上下文
- 会话级 resume context
- 这些层之间的优先级和注入边界还可以更清晰

#### 更强的“工具语义统一”
- 工具失败结构统一
- 失败是否可恢复统一
- 需要确认时的提示统一
- 读写编辑命令的响应语气统一

#### 更强的“继续工作感”
- `resume` 后自动带上当前目标、最近计划、上次失败、验收标准
- 让 agent 真正接着上次继续做，而不是只恢复状态

#### 更完整的命令工作流
- `/help`
- `/status`
- `/resume`
- `/sessions`
- `/rollback`
- `/memory`
- `/compact`
- `/model`
- `/mode`

这些命令现在都有雏形，但还没有形成完全统一的用户手感。

## 4. 对照表

| 能力 | Claude Code | 本项目现状 | 结论 |
|---|---|---|---|
| 任务执行闭环 | 很强 | 已有 | 基本具备 |
| 会话恢复 | 很强 | 已有 | 接近可用 |
| Checkpoint / rollback | 很强 | 已有 | 接近可用 |
| 权限与审批 | 很强 | 已有 | 方向对，仍需收口 |
| Skills / plugins | 很强 | 已有 | 需要继续打磨 |
| 上下文层级 | 很强 | 部分具备 | 仍需补齐 |
| 终态输出 | 很强 | 部分具备 | 仍需统一 |
| 命令体验 | 很强 | 部分具备 | 仍需产品化 |
| 多代理协作 | 很强 | 已有 | 结构已成立 |
| 验证与修复 | 很强 | 已有 | 需要更严格的约束 |

## 5. 建议的优先 TODO

### P0
1. 统一上下文加载顺序
2. 统一权限/审批语义
3. 统一 resume context 结构
4. 统一 final / trace / session summary 口径
5. 统一工具失败格式

### P1
1. 完整化 `/memory` 视图
2. 完整化 `/sessions`、`/rollback` 输出
3. 收口 `CLAUDE.md` / prompt builder 规则
4. 强化 verifier 的验收约束
5. 强化 subagent 的隔离与回收

### P2
1. 改善技能发现和命名空间冲突
2. 改善插件安装 / 启用 / 卸载体验
3. 做更完整的 trace 浏览和回放
4. 做更细的终端提示和错误分层

## 6. 学习路径

如果你是想学习 Claude Code 的思路，建议按这个顺序看：
1. 它如何理解任务
2. 它如何加载项目上下文
3. 它如何决定是否需要确认
4. 它如何调用工具并处理失败
5. 它如何保存并恢复会话
6. 它如何把结果总结成可继续工作的状态

## 7. 对本项目的结论

本项目已经不是“demo 级别”，而是一个**正在向 Claude Code 靠拢的本地多代理开发工作台**。
真正差的不是“有没有功能”，而是：
- 语义统一性
- 恢复连续性
- 终态摘要一致性
- 工具与权限的产品化程度

这份文档可以继续作为：
- 学习材料
- 产品对照表
- 后续开发 TODO 清单

## 8. 下一步

如果要继续补，建议下一阶段只做核心功能，不再扩 UI：
- 完整化 `CLAUDE.md` / memory 层级
- 完整化 `status / sessions / rollback / memory`
- 强化 resume context
- 强化 verifier gate
- 强化 tool error 统一

