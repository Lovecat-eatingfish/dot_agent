# Claude Code 核心对齐清单（详细版）

> 目的：只补核心，不扩 UI。用于学习，也用于后续开发 TODO。

## 0. 总体目标

Claude Code 的价值不在聊天，而在它能持续地、稳定地、可恢复地完成代码工作。
本项目当前已经具备多代理、记忆、权限、checkpoint、trace、skills、插件等骨架，下一步应该把这些骨架收敛成更统一的工作流。

## 1. 上下文加载链

### Claude Code 是怎么做的
- 自动加载项目和用户内存
- 从当前目录向上递归读取 `CLAUDE.md` / `CLAUDE.local.md`
- `CLAUDE.md` 支持 `@path/to/file` 导入
- 导入可以递归，但有最大深度限制
- `/memory` 可以查看加载了哪些内存文件

### 本项目现状
- 已有 `CLAUDE.md` 风格配置
- 已有 `PromptBuilder`
- 已有 session context 注入
- 已有 layered memory
- 但“加载顺序 / 来源 / 优先级 / 可见性”还不够统一

### 需要补齐的点
1. 统一 memory 发现规则
2. 统一 import 递归规则
3. 统一项目级 / 用户级 / 会话级 / 运行时的优先级
4. `/memory` 显示“来源 + 层级 + 是否已加载”
5. 让所有节点用同一条上下文链

### 验收标准
- 同一个 workspace 在不同子目录启动时，加载的 memory 行为一致
- 用户能清楚看见当前记住了什么
- 新增的 memory 文件不会在别的层级中造成歧义

## 2. 权限与工具语义

### Claude Code 是怎么做的
- `--permission-mode`
- `--allowedTools`
- `--disallowedTools`
- 高危命令会先解释，再确认
- 拒绝、允许、确认、危险四类语义清晰

### 本项目现状
- 已有 `agent_mode`：`auto / plan / approve / edit / bypass`
- 已有 `approval_mode`：`inline / auto / deny`
- 已有 `tool_gate`
- 已有危险 bash 拦截
- 已有工具失败的结构化返回，但各处还不完全统一

### 需要补齐的点
1. 统一工具失败返回 schema
2. 统一可恢复/不可恢复字段
3. 统一确认请求结构
4. 增加显式 allowed/disallowed tool 语义
5. 做 `/permissions` 视图

### 验收标准
- 所有 mutating 工具都有统一的错误语义
- 用户一眼能看懂为什么被拦、如何继续
- 能从命令层清楚知道当前允许什么、禁止什么

## 3. Resume / Continue

### Claude Code 是怎么做的
- `--continue` 继续最新会话
- `--resume <id>` 恢复指定会话
- 恢复后更像继续同一个思路
- 恢复时自动带上前文和当前目标

### 本项目现状
- 已有 session store
- 已有 turn checkpoint
- 已有 resume context
- 已有 trace summary
- 但“继续感”还可以更强

### 需要补齐的点
1. 恢复时自动注入最近计划和验收标准
2. 恢复时自动注入 verifier/repair 摘要
3. 恢复时自动说明当前处于哪一轮
4. `continue` 与 `resume` 做明确语义区分
5. 恢复入口直接显示当前思路摘要

### 验收标准
- 用户输入“继续”，系统能接着上次的工作线跑
- 恢复后第一轮就能知道上次卡在哪
- 不需要重新解释整个任务

## 4. 终态输出

### Claude Code 是怎么做的
- 终态总结短、准、可继续
- 不追求长篇流水账
- 重点是结果、验证和下一步

### 本项目现状
- 已有 final summary
- 已有 trace summary
- 已有 session summary
- 已开始统一卡片风格
- 但三者口径还可以继续统一

### 需要补齐的点
1. final / trace / session summary 共用一套字段
2. 统一 plan / todo / verifier / repair / sources / next step
3. 统一成功 / 失败文案
4. 统一摘要长度和折叠方式
5. 统一“下一步建议”的生成方式

### 验收标准
- 用户从任意摘要里都能看懂：做了什么、成没成、下一步是什么
- 多处 summary 不再说法不一致

## 5. `/memory` 和工作流视图

### Claude Code 是怎么做的
- `/memory` 直接看和编辑内存
- 能理解当前加载了哪些记忆
- 能把项目约定、个人偏好、导入文件放在一套可解释的视图里

### 本项目现状
- 已有 memory / topic store / layered memory
- 已有会话摘要和历史压缩
- 但 `/memory` 还是偏概念展示

### 需要补齐的点
1. 显示 memory 来源
2. 显示 memory 层级
3. 显示当前 session 依赖了哪些 memory
4. 显示哪些 memory 文件被导入
5. 支持看懂“当前为什么这样工作”

### 验收标准
- `/memory` 能帮助用户定位规则来自哪里
- 用户能快速修改和验证项目记忆

## 6. 其他核心能力

### 6.1 工具调用可靠性
- 读工具并发
- 写工具串行
- 错误结构统一
- 输出可回放

### 6.2 verifier 门禁
- verifier 不只是“看起来对”
- verifier 应该基于验收标准和检查结果
- 失败后要给出可执行的 repair instruction

### 6.3 subagent 能力
- 独立上下文
- 明确职责
- 可中断、可轮询、可回收
- 不无限嵌套

### 6.4 插件和技能
- 技能发现
- 命名空间
- 冲突处理
- 懒加载
- 插件安装 / 启用 / 卸载

## 7. 最优先的 5 项 TODO

### P0
1. 统一上下文加载链
2. 统一权限与工具语义
3. 强化 resume / continue
4. 收敛终态输出口径
5. 做深 `/memory`

### P1
1. `/permissions` 命令
2. 完整的 allowed / disallowed tools 语义
3. 子目录 `CLAUDE.md` 规则
4. 更强的 verifier 门禁
5. 更完整的 subagent 管理

### P2
1. 插件生命周期完善
2. MCP 工具管理完善
3. trace 浏览和回放
4. session 压缩策略继续优化

## 8. 建议的实现顺序

1. 上下文加载链
2. 权限语义
3. resume / continue
4. 终态输出统一
5. `/memory`

这是最像 Claude Code 的路线，也最能把当前项目从“有骨架”推进到“可持续工作台”。
