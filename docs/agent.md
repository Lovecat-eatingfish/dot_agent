# 工作流 vs 代理
Anthropic 区分两种代理系统类型：

类型	定义	特点
工作流 (Workflows)	LLM 和工具通过预定义代码路径进行编排的系统	确定性、可预测
代理 (Agents)	LLM 动态指导自身流程和工具使用的系统	自主性、灵活性



# 生产模式
## 意图识别：
1. chat
2. 复杂任务流

## 任务拆解

将任务分解为顺序步骤，每一步都有质量检查门。

输入 → [步骤1] → 检查 → [步骤2] → 检查 → [步骤3] → 输出
适用场景：

多步骤文档处理
复杂数据转换
分阶段内容生成

## 任务检查
将任务分解为顺序步骤，每一步都有质量检查门。


## 路由

对输入进行分类，将其导向专门的下游处理流程。

```text
        ┌──→ [处理器A] ──┐
输入 → [分类器] ──→ [处理器B] ──→ 输出
        └──→ [处理器C] ──┘
```

适用场景：

客服系统
多领域问答
任务分发


## 并行化
参考： llm并行 / function 并行化

同时运行多个 LLM 调用以提高速度或通过投票达成共识。

两种模式：

分段 (Sectioning)：将任务分成独立部分并行处理
投票 (Voting)：多个模型处理同一任务，综合结果
适用场景：

大规模文档分析
多角度评估
高可靠性任务


## 多agent协同
中央 LLM 动态地将不可预测的子任务委派给工作者。

```text
                    ┌──→ [工作者1]
[编排者] ──委派──→ [工作者2] ──汇总──→ [编排者]
                    └──→ [工作者3]
```
适用场景：

复杂编码任务
多文件重构
研究型任务

问题： 
1. 数据同步
2. 数据传递问题


## 评估和优化
一个 LLM 生成响应，另一个提供迭代反馈。

[生成器] → 输出 → [评估器] → 反馈 → [生成器] → ... → 最终输出

适用场景：

代码优化
文案润色
翻译校对



## 可恢复性


## 可观测性


## 权限管理


## 可扩展性
1. mcp
2. skill
3. cluade.md
4. 自定义命令
5. 思考模式的切换：对应system提示词的 添加
思考模式关键词：

think - 基础思考
think hard - 深入思考
think harder - 更深入思考
ultrathink - 最深度思考

6. 插件机制
推荐插件
插件	功能
code-review	自动化 PR 审查
commit-commands	Git 工作流
frontend-design	前端设计指导
security-guidance	安全监控


# 自主代理
自主代理独立运行在循环中，需要：

✅ 清晰的成功指标 / 和计划
✅ 沙盒环境： 操作的文件目录必须 不能飘逸
✅ 适当的人工监督点： 反馈点





# 上下文工程
模型的注意力预算是有限的


## 关键技巧
1. 即时检索 (Just-in-Time Retrieval)
这模仿了人类认知——使用外部索引系统而不是记忆整个语料库。


```text
传统方法：
┌─────────────────────────────────┐
│ 预加载所有可能需要的数据到上下文 │
└─────────────────────────────────┘

即时检索方法：
┌──────────────┐    需要时    ┌──────────────┐
│ 轻量级标识符 │ ──────────→ │ 动态检索数据 │
└──────────────┘              └──────────────┘
```

2. 压缩 (Compaction)
当接近上下文限制时总结对话历史：

保留：

架构决策
关键细节
重要结论
丢弃：

冗余工具输出
中间推理步骤
重复信息



3. 结构化笔记 (Structured Note-Taking)
让代理维护持久的外部记忆：


# progress.txt

## 会话 3 进度
- ✅ 修复了认证令牌验证
- ✅ 更新用户模型以处理边缘情况
- 🔄 下一步：调查 user_management 测试失败
- ⚠️ 注意：不要删除测试，可能导致功能缺失

## 关键决策
- 选择使用 JWT 而非会话认证
- 数据库使用 PostgreSQL
这对于跨越多小时的任务特别有用，能够在上下文重置后保持连续性。



4. 子代理架构 (Sub-Agent Architectures)
专门的代理处理聚焦的任务，返回精简摘要：


```text
┌─────────────────────────────────────────┐
│              主代理                      │
│  (管理整体任务，保持精简上下文)              │
└─────────────┬───────────────────────────┘
              │
    ┌─────────┼─────────┐
    ↓         ↓         ↓
┌───────┐ ┌───────┐ ┌───────┐
│子代理1│ │子代理2│ │子代理3│
│(搜索) │ │(分析) │ │(编码) │
└───┬───┘ └───┬───┘ └───┬───┘
    │         │         │
    └────→ 精简摘要 ←────┘
              │
              ↓
         返回主代理
```
优势：

每个子代理有干净的上下文
只返回精华信息
避免主上下文污染



# 任务的不同选择不同的策略

策略	                最适合场景
压缩 (Compaction)	需要保持对话流的长时间来回交互
笔记 (Note-taking)	有明确里程碑的迭代开发
多代理 (Multi-agent)	并行探索有价值的复杂研究任务

随着模型能力增强，挑战不再只是编写完美的提示——而是深思熟虑地管理进入模型有限注意力预算的信息。




# 沙盒
Claude Code沙盒本质 = 一个临时工作目录 + 一组环境变量 + 进程级资源限制

拆解本质
层面	        本质	         实现方式
文件系统	    临时文件夹	 每次会话创建独立目录（如 /tmp/claude-xxxxx/），会话结束即删除
环境变量	    隔离的env	 每个沙盒有独立的 PATH、PYTHONPATH、API_KEY 等
网络/进程	进程隔离	     每个沙盒作为独立进程运行，有独立的PID、内存限制、CPU配额



# claude code 理解
## ai发展
1. prompt工程： few-shot, CoT, system prompt 模板 （听懂
2. 上下文工程： RAG, embedding 检索, 长上下文模型 （知道
- 如何存储： 本地文件
- 如何检索： grep glob
- 如何管理： 结构化， 隔离， 压缩（直接截断 / 多阶段压缩，理性压缩）
3. harness 工程： Claude Code, Codex CLI, Cursor Agent, Devin （超级赛亚人

### harness
是 2026 年 AI 工程投资的主轴：模型本身正在快速商品化，但模型外层的 harness——permission gating、tool dispatch、context compaction、subagent isolation、hook lifecycle——才是真正决定 Agent 在生产环境能否长时运行而不漂移的关键。



## 架构

1. 统一入口： cli，app， web，插件
2. harness控制： system hook， 权限控制，REPL循环， 状态管理
3. 引擎： loop单例的， sse流管控
4. 工具&能力，外部继承扩展：mcp skill subagent plugin/市场  hooks
5. 


### 五层
Entrypoints / Runtime / Engine / Tools&Caps / Infrastructure


源码显示 Claude Code 采用清晰的五层分层设计：

层级	职责	关键组件
Layer 1: Entrypoints	多端入口	CLI / Desktop / Web / SDK / IDE Extensions / Headless
Layer 2: Runtime	运行时核心	REPL loop / Hook system / Permission engine / State manager
Layer 3: Engine	推理引擎	QueryEngine / Context coordinator / Compact pipeline / Streaming demux
Layer 4: Tools & Caps	能力层	40+ tools / Plugin / MCP / Skill / Sub Agent / Hooks
Layer 5: Infrastructure	基础设施	Auth / Storage / Cache / Bridge transport / Telemetry / Sandbox


Layer 3 是模型可见的边界——之上对模型透明，之下模型完全感知不到；Layer 2 是 harness 真正的控制权所在，Hook 与 permission 在这里强制执行。



## Runtime
### 提示词缓存
Claude Code 不是一句简单 prompt，是模块化 Agent 系统提示词 + Tools 工具定义 + 动态上下文注入 + 缓存策略，官方没有放出完整原始源码，但社区逆向拆解出完整架构，可以复刻一套自己的代码 Agent。

```json
{
  "model":"claude‑3‑5‑sonnet",
  "system":[
    {"type":"text","text":"【静态模块：身份、规则、工具用法】","cache_control":{"scope":"global"}},
    {"type":"text","text":"__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"},
    {"type":"text","text":"【动态模块：目录、git状态、CLAUDE.md、操作系统】","cache_control":null}
  ],
  "tools":[/* Read / Edit / Bash / Glob / Grep / AskUserQuestion … */],
  "messages":[...]
}
```
- 静态区：不变，命中缓存，省 token、提速 
- 动态分界线：__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__，后面每次会话刷新环境信息 
- 工具定义：API 的 tools 参数，不是写在 system 文本里！这点很多人复刻踩坑。


工程上的关键约束：提示词的排列顺序至关重要。前缀匹配是字节级的——任何在前缀中发生的细微改变，都会击穿该位置之后的所有缓存。tool 定义改了，整条都失效；system 改了，messages 部分失效；改 tool_choice 或加图片，则 system 之后的部分全部失效。







Claude Code 最具工程价值的设计之一，是将系统提示词拆分为静态部分和动态部分，并围绕 Anthropic API 的 prompt caching 机制构建了完整的缓存优化策略。
1. 静态的东西 就是和系统图交互的东西，所有用户共享， 

#### 静态提示词
```text
静态提示词 7 大模块（顺序不能乱）

1. 身份 & 安全策略 Intro
   - 你是一个软件工程师 Agent Claude Code。
   - 只能做开发相关；禁止破坏性操作；不能猜测 URL；授权测试可以做。

2. 系统输出规则 System
   - Markdown 输出规范。
   - 处理工具报错。
   - 处理 prompt 注入。
   - 上下文压缩规则。
   - 权限模式（是否需要用户确认高危操作）。

3. 任务执行规则 Doing Tasks（最重要工程约束）
   - ✅ 修改代码前必须先Read读文件，不能幻觉写代码。
   - ✅ 优先增量Edit，不要整个重写文件。
   - ✅ 失败先诊断，不要盲目重试。
   - ✅ 只做用户要求的，不要过度设计、不要提前抽象。
   - ✅ 修改完要验证，测试、重读文件确认生效。
   - ❌ 不要给时间预估；不要引入无关改动。

4. 风险分级 Executing Actions with Care
   - 可撤销操作（本地文件、单元测试）直接执行。
   - 高危操作（force-push、改生产、外网发送）必须向用户确认。

5. 工具使用指南 Using Tools
   - 优先 Read，少用 cat。
   - 优先 Grep 搜索。
   - glob 遍历目录。
   - edit 做局部修改，write 用于新建文件。

6. 输出格式约定
   - 工具调用走 API tool_use，不要自己写伪标签。
   - 自然语言尽量简短，把细节留给工具。

7. 错误处理
   - 工具返回报错，先分析日志，不要直接换方案。
   - 阻塞时创建子任务，不要标记任务完成。
```


#### 动态提示词
memory: claude.md 加上 @import 引用
当前工作目录、操作系统、日期
git status、变更文件列表
项目根目录 CLAUDE.md（项目专属规则、技术栈、编码规范）
MCP 服务列表、会话记忆、用户偏好
CLAUDE.md 是 Claude Code 特色：放在项目根，相当于项目专属 prompt，每次注入动态区，告诉 Agent 本项目的技术栈、目录约定、禁用写法。


```js
const dynamicSections = [
  systemPromptSection('session_guidance', ...),
  systemPromptSection('memory', ...),         // CLAUDE.md / @import
  systemPromptSection('env_info_simple', ...), // 系统/cwd/git 状态
  systemPromptSection('language', ...),
  systemPromptSection('output_style', ...),
  systemPromptSection('mcp_instructions', ...), // 不可缓存
  systemPromptSection('scratchpad', ...),
]

```


#### eg
```text
You are Claude Code, a professional software‑engineering agent.

# Core Rules
1. ALWAYS read relevant files BEFORE editing. Never hallucinate file content.
2. Prefer incremental edit over full file rewrite.
3. After making changes, re‑read files or run tests to verify correctness.
4. Do only what the user asks. No extra refactors or premature abstractions.
5. If tools return errors, diagnose logs before retrying. Ask user when blocked.
6. Dangerous operations require explicit user confirmation.

# Tool Guidance
- Use read to view source files.
- Use edit for partial file modification.
- Use glob to discover files.
- Use grep for code search.
- Use bash to run build/test commands.
- Use ask_user_question when information is missing.

__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
Current working directory: {{cwd}}
OS: {{os_name}}
Project rules from CLAUDE.md: {{claude_md_content}}
```


#### tool

核心工具：
read：读取文件
edit：局部文件编辑（diff 式修改，不是全量覆盖）
write：新建 / 覆写文件
glob：通配符找文件
grep：代码库搜索
bash：执行 shell 命令
ask_user_question：向用户提问获取信息
task：子 Agent 任务调度（Coordinator 多代理模式）
⚠️ 复刻大坑：很多人把工具描述写进 system 字符串，正确做法是使用 Anthropic API 原生 tools 字段，模型原生 tool_use 调用。


#### 子智能体如何继承缓存




#### 如何实现的
Anthropic 的 Prompt Caching 是 Claude 3.5 Sonnet / Opus 专属能力，核心：把大段不变的 system /message 前置内容缓存到 Anthropic 服务端，后续请求不用重复传输这部分 token，降成本、提速。
Claude Code 重度依赖它，把上万 token 静态系统提示词做全局缓存，动态部分每次重新发送。

原生的kvcache：
- 底层：原生 KV Cache（推理引擎内部）
- 所有 Transformer 推理引擎（vLLM、TensorRT‑LLM、Anthropic 内部推理服务）自带的机制。
- 作用范围：同一个 API 请求内部
- prefill 阶段：输入全部 token，每一层 Transformer 算出每个 token 对应的 K、V 张量，存 GPU 显存。
- decode 生成 token 的时候，直接复用显存里已经算好的 K/V，不用重新算全部历史 token，大幅加速生成。
- 生命周期：这个 API 请求结束，KV 直接丢弃释放显存，不能跨多次 http 请求复用

> 普通调用 Claude，每发一次 http 请求，就完整跑一遍 prefill，算一遍完整 system prompt 的 KV，跑完就扔。Claude Code 每次对话几十轮，如果没有跨请求缓存，上万 token 的 system 每轮都重算，成本爆炸、延迟很高。


底层就是把推理引擎输出的 KV Cache，做跨请求持久化保存，不是存原始文本，存的是多层 transformer 算出来的 K/V 张量，附带 token 序列哈希做匹配。


```text
================================================================================
                         Prompt Cache 机制 - 完整指南
================================================================================

【1. 核心概念】
================================================================================

1.1 cache_control 标记
    cache_control: {"scope": "global"}
    → 标记这段文本进入服务端缓存，跨会话复用。

1.2 缓存单元格式
    必须是数组里独立的 {"type":"text", "text":"xxx", "cache_control":{...}} 对象
    → 不能是普通字符串 system
    → ❗ system 参数不能传字符串，必须传数组格式，否则缓存不生效。

1.3 缓存有效期
    5 分钟
    → 5 分钟内有新请求命中同一块缓存就复用
    → 超时自动失效，需要重新上传一次。

1.4 最低缓存块大小
    最少 1024 tokens
    → 不足这个 token 数不会触发缓存
    → 这是硬限制，短片段打 cache 标记没用。


【2. 两种 scope 类型】
================================================================================

2.1 scope: "global"（Claude Code 主要用这个）
    → 缓存块可以跨多条消息轮次复用
    → 适合静态系统提示词、工具大段描述、项目固定知识库
    → Claude Code 用法：
        - 静态规则、工具说明全部打 global
        - 分界线之后动态内容不加 cache_control
        - 每次随请求全新发送

2.2 scope: "ephemeral"
    → 只在当前单次 API 请求内生效
    → 不会保存到后续轮次
    → 适合本次对话临时大上下文


【3. Claude Code 缓存结构拆解（真实结构）】
================================================================================

{
    "model": "claude-3-5-sonnet-20241022",

    "system": [
        {
            "type": "text",
            "text": "【上万token静态Claude Code系统提示词、全部工具使用规则】",
            "cache_control": {"scope": "global"}
        },
        {
            "type": "text",
            "text": "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
            // ⚠️ 这里不打cache，作为分割边界
        },
        {
            "type": "text",
            "text": "cwd=/xxx os=linux git status=xxx CLAUDE.md内容..."
            // 动态环境信息，每次请求重新生成，无cache_control
        }
    ],

    "tools": [...],
    "messages": [...]
}

关键点：
    → 分界线把 system 切为两块
    → 前面巨大静态块被缓存
    → 后面环境、git、claude.md 每次刷新，不走缓存


【4. messages 消息也可以做缓存】
================================================================================

说明：
    → 不光 system，历史消息数组同样可以加 cache_control
    → 一般把最前面一大段历史打上 global 缓存
    → 后面新消息不带缓存

示例：
    "messages":[
        {
            "role":"user",
            "content":[
                {
                    "type":"text",
                    "text":"很长的项目代码上下文...",
                    "cache_control":{"scope":"global"}
                }
            ]
        },
        // 后面是本轮新增简短对话，不缓存
        {"role":"user","content":"帮我修复这个bug"}
    ]


【5. 返回值怎么看缓存有没有命中】
================================================================================

看 API 返回 usage 对象：

    "usage": {
        "input_tokens": 120,
        "cache_creation_input_tokens": 12400,    // 第一次：上传缓存块，计费这个
        "cache_read_input_tokens": 0,
        
        // ✅ 后续命中缓存时：
        "input_tokens": 130,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 12400         // 读取缓存token，价格便宜很多
    }

字段含义：
    cache_creation_input_tokens
        → 没命中，上传缓存块，完整计费

    cache_read_input_tokens
        → 命中缓存，读取缓存
        → 价格约普通输入 token 的 1/10

生命周期：
    第一次调用 → 创建缓存
    5 分钟内再次调用 → 读缓存
    超过 5 分钟 → 重新创建


【6. 高频踩坑（复刻 Claude Code 必踩）】
================================================================================

❌ 踩坑 1：system 传普通字符串
    // 错误
    "system": "一大段文本..."

    // 正确
    "system": [{"type":"text","text":"...","cache_control":{"scope":"global"}}]
    → 不是数组对象格式 → cache 完全无效

❌ 踩坑 2：缓存块 token 不足 1024
    → 加标记也不会生效
    → Claude Code 静态 system 上万 token 刚好满足

❌ 踩坑 3：动态变化的内容打上 global 缓存
    → 比如每次变的 git 状态、当前目录
    → 千万不要缓存，会读到旧的过期信息

❌ 踩坑 4：缓存块中间插入动态内容
    → 一旦被标记 global 的文本块内部有任何字符改动
    → 缓存直接失效，会重新 creation
    → 必须用分界线分割：静态块完全不动，动态放后面独立对象

⚠️  踩坑 5：缓存只有 5 分钟
    → 长时间空闲后再次请求，会重新触发 cache_creation

❗  踩坑 6：tools 参数不会被 prompt cache 缓存
    → tools 是独立 API 参数，不是 system 文本
    → 大 tools schema 每次请求都要完整传


【7. Python 极简调用示例（anthropic sdk）】
================================================================================

要求：anthropic >= 0.32.0

import anthropic

client = anthropic.Anthropic()

static_system_prompt = """
你是Claude Code代码Agent......这里一大段静态规则，超过1024token
"""

dynamic_part = f"""
__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
cwd=/home/project
OS: Linux
"""

resp = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    system=[
        {
            "type": "text",
            "text": static_system_prompt,
            "cache_control": {"scope": "global"}
        },
        {
            "type": "text",
            "text": dynamic_part
        }
    ],
    messages=[{"role":"user","content":"帮我写一个函数"}],
    tools=[...],
    max_tokens=2048
)

print(resp.usage)


【8. Claude Code 缓存工程技巧】
================================================================================

技巧 1：静态 system 永远完全不变
    → 一个字符都不要改，保证缓存命中率
    → 自定义规则放到动态区 CLAUDE.md
    → 不要修改静态块

技巧 2：不要把每次会变的变量塞到打 cache 的 text 块中

技巧 3：长会话 messages 缓存策略
    → 把早期大代码上下文打 global 缓存
    → 最新几轮消息不缓存

技巧 4：监控缓存命中率
    → 监控 cache_read_input_tokens
    → 如果一直是 0，说明缓存没生效
    → 排查：token 数量、数组格式、文本是否变动

================================================================================
```

model: 'inherit' 的核心价值在于：Fork 出的子智能体继承父智能体的完整对话上下文，通过 byte-identical copies 实现提示词缓存共享。子任务必须使用和父对话相同的 prompt prefix，才能复用父对话的缓存。这使得生成 5 个并发子智能体的成本仅略高于 1 个。inherit 的解析有四级优先级：环境变量 CLAUDE_CODE_SUBAGENT_MODEL → 调用时显式 model 参数 → frontmatter 的 model → 主对话 model。


[PATTERN] 缓存继承模式：子智能体不要"另起炉灶"——通过 model: 'inherit' 字节级对齐父对话 prompt prefix，复用父对话已经付费写入的缓存。把"派生子智能体"做成"复用上文"，5 个并发的成本接近 1 个。


1： 父不会传递 KV 缓存，父只传递两件东西给子 Agent 实例：
- 原始静态前缀字符串文本（字节原样复制，不能做任何修改）
- 继承标记 model:inherit，告诉子任务构造器：不要重新生成 system 静态部分，直接拷贝这份前缀。

子 Agent 是独立任务，独立 HTTP 调用，只是请求 payload 的头部文本和父完全对齐。


```python
# 父agent运行后，保存好原始静态块（一字不变）
parent_static_system_block = {
    "type":"text",
    "text":"上万token静态规则...",
    "cache_control":{"scope":"global"}
}

def fork_sub_agent(inherit:bool, sub_task_prompt:str):
    if inherit:
        # inherit模式：直接复用父的静态块，字节完全复制
        static_block = deepcopy(parent_static_system_block)
    else:
        # 普通模式：重新生成一套完整静态prompt
        static_block = build_fresh_static_system()

    # 分界线固定不变
    boundary_block = {"type":"text","text":"__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"}

    # 子任务专属动态部分，每个子不一样
    sub_dynamic_block = {
        "type":"text",
        "text":f"SUB_TASK:{sub_task_prompt}"
    }

    system_payload = [static_block, boundary_block, sub_dynamic_block]
    # 子发起全新独立API请求
    return anthropic.messages.create(system=system_payload, ...)
```


二： 注意条件
- 致命约束（inherit 能生效的前提）
- 静态块字节必须完全一致，多一个空格、换行、注释改动，token 序列改变，缓存直接失效，退化成 N 倍成本。
- 必须在缓存 TTL 窗口内（5 分钟）Fork 子任务；如果父的缓存已经过期，子请求会触发新的 cache_creation。
- tools 参数不会进入 prompt cache，每个子 Agent 依旧要完整携带 tools 定义，这部分无法省。
- 父、子用同一个 model，不能换模型；不同模型 KV 缓存不互通。



## 记忆管理
### 分类
类型         存储位置               用途
短期记忆	    会话内对话状态数组	      当前对话上下文
中期记忆	    ~/.claude/memory/	  用户偏好、常用命令模式
长期记忆	    DreamTask后台整合	  跨项目经验积累


### 文件
记忆系统让 Agent 在后续会话中自动加载历史偏好，表现出"越来越懂你"的行为。/memory 命令读写 MEMORY.md，约定首 200 行或 25KB 加载——和 CLAUDE.md 同样以 user message（不是 system prompt）形式注入。

```text
================================================================================
                   Claude-Code Memory 文件清单完整总结
================================================================================


【核心概念一句话】
================================================================================

MEMORY.md = 索引清单（只存指针，进 system）
*.md = 主题记忆文件（存正文，按需 read）
session-memory/ = 临时会话摘要（不跨会话）


【一、三层存储路径总览】
================================================================================

┌─────────────────┬─────────────────────────────────────────────┬──────────────┐
│  层级           │  路径                                       │  生命周期    │
├─────────────────┼─────────────────────────────────────────────┼──────────────┤
│  用户全局       │  ~/.claude/user-memory/                     │  跨项目永久  │
├─────────────────┼─────────────────────────────────────────────┼──────────────┤
│  项目级         │  ~/.claude/projects/<project-id>/memory/    │  跨会话永久  │
├─────────────────┼─────────────────────────────────────────────┼──────────────┤
│  会话临时       │  ~/.claude/projects/<pid>/<sessionId>/      │  单会话临时  │
│                 │  session-memory/                            │              │
└─────────────────┴─────────────────────────────────────────────┴──────────────┘


【二、每层文件清单】
================================================================================

2.1 用户全局（~/.claude/user-memory/）
    ├── MEMORY.md          # 全局索引指针，注入 system
    ├── preferences.md     # 用户偏好（可不断新增）
    ├── coding_style.md    # 编码风格
    └── *.md               # 其他任意主题文件，无限扩展

2.2 项目级（~/.claude/projects/<project-id>/memory/）
    ├── MEMORY.md          # 项目索引指针，注入 system
    ├── project_decisions.md   # 架构决策
    ├── feedback.md        # 用户反馈记录
    ├── tech_stack.md      # 技术栈信息
    ├── *.md               # 其他任意主题，无限扩展
    └── team/*.md          # 团队共享记忆子目录

2.3 会话临时（~/.claude/projects/<pid>/<sessionId>/session-memory/）
    └── summary.md         # 仅当前会话，用于压缩，不持久化


【三、文件数量公式】
================================================================================

最小状态（空记忆）：
    2 个索引（用户 + 项目）
    0 个主题
    1 个会话摘要

使用后：
    索引 = 固定 2 个
    主题 = 用户 N 个 + 项目 M 个（随记忆增长无限扩展）
    会话摘要 = 每会话 1 个（用完即弃）


【四、system 注入顺序（重要）】
================================================================================

用户 CLAUDE.md → 项目 CLAUDE.md → 用户 MEMORY.md（索引） → 项目 MEMORY.md（索引）

全部进入 system 动态块，model:inherit 子 Agent 可继承。

⚠️ 主题 *.md 不注入 system，模型按需 read() 读取。


【五、容易混淆的边界】
================================================================================

CLAUDE.md ≠ Memory
    → CLAUDE.md：人写的静态规则，AI 只读
    → Memory/*.md：AI 自动读写的动态记忆

MEMORY.md ≠ 记忆正文
    → 它只是索引指针清单
    → 真实内容在各主题 *.md 中

session-memory/ ≠ 持久记忆
    → 只服务于当前会话，不写入 MEMORY.md
    → 跨会话不可见

用户全局 ≠ 项目记忆
    → 用户全局：本机所有项目共用
    → 项目级：仅当前项目生效，两者合并加载

================================================================================
```




### 如何实现
```text
================================================================================
                   Claude-Code Memory（记忆系统）完整实现
================================================================================


【核心定位】
================================================================================

核心要点：
    → 没有向量数据库
    → 全部本地磁盘 Markdown 文件实现
    → 分层设计
    → 靠后台 forked 子 Agent 做记忆提取，不阻塞主对话
    → 索引 + 主题文件分离模式
    → 会话启动加载索引，主题文件按需 read 读取


记忆系统区分：
    CLAUDE.md：静态手写规则，人写，AI 只读
    Auto Memory（memdir）：AI 自动读写的跨会话记忆
    Session Memory：仅当前会话的会话摘要
    AutoDream：后台 "记忆整理" 后台任务
    /memory 斜杠命令：记忆管理入口


【一、磁盘目录结构】
================================================================================

    ~/.claude/projects/<项目唯一标识>/memory/
    ├── MEMORY.md                # 📌 索引文件，每次会话启动加载前200行/25KB上限
    ├── user_role.md             # 记忆主题文件（带YAML frontmatter）
    ├── feedback_prefer.md
    ├── project_decisions.md
    └── team/                    # 团队共享记忆目录

    # 会话临时记忆（会话ID隔离）
    ~/.claude/projects/<项目>/<sessionId>/session-memory/
    └── summary.md


1.1 MEMORY.md 索引文件

    → 只存索引指针，不存记忆正文

    格式示例：
        - [用户偏好](feedback_prefer.md) — 用户不希望使用Lombok
        - [项目架构](project_decisions.md) — 后端使用SpringBoot3

    限制：
        → MEMORY.md 最大 200 行 / 25KB
        → 超出直接截断
        → 防止索引膨胀把 system prompt 撑爆


1.2 记忆主题文件

    → 真正记忆内容分散在各个 xxx.md 主题文件
    → 每个记忆文件头部带 YAML frontmatter
    → 标记类型：user / feedback / project / reference


1.3 关键设计：索引常驻 system，主题文件按需 read

    → 会话启动：把 MEMORY.md（索引）注入 system 动态块
        - 模型看到 "现在有哪些记忆主题"

    → 但不会把所有 xxx.md 全部读进上下文
        - 模型需要哪条记忆，自己调用 read() 读取对应的主题文件

    解决痛点：
        → 记忆越积越多
        → 不会每次会话把全部记忆塞进 token 窗口


【二、四层记忆分层】
================================================================================

┌─────────────┬─────────────────────────┬───────────────────┬─────────────────────────────────────────┐
│  层级       │  名字                   │  存储             │  作用                                   │
├─────────────┼─────────────────────────┼───────────────────┼─────────────────────────────────────────┤
│  1          │  CLAUDE.md              │  项目/用户目录    │  静态硬规则                             │
│             │                         │  md 文件          │  会话启动完整注入 system                 │
│             │                         │  永久，人维护     │  AI 不能修改                            │
├─────────────┼─────────────────────────┼───────────────────┼─────────────────────────────────────────┤
│  2          │  Auto Memory（memdir）  │  memory/*.md      │  自动记录用户偏好、项目决策、踩坑经验   │
│             │                         │  磁盘文件         │  索引 MEMORY.md 进 system               │
│             │                         │  跨会话，AI 读写  │  主题按需 read 读取                     │
├─────────────┼─────────────────────────┼───────────────────┼─────────────────────────────────────────┤
│  3          │  Session Memory         │  session-memory/  │  当前会话摘要                           │
│             │                         │  summary.md       │  用于上下文压缩                         │
│             │                         │  仅当前会话       │  会话结束丢弃                           │
├─────────────┼─────────────────────────┼───────────────────┼─────────────────────────────────────────┤
│  4          │  AutoDream              │  后台 forked      │  记忆合并、去重、修剪旧记忆             │
│             │                         │  agent 输出到     │  后台异步做                             │
│             │                         │  memdir           │  不阻塞对话                             │
│             │                         │  跨会话           │                                        │
└─────────────┴─────────────────────────┴───────────────────┴─────────────────────────────────────────┘


【三、记忆写入两条路径】
================================================================================

3.1 路径 1：用户显式要求记住（主 Agent 直接写）

    用户：记住：本项目禁止使用 Lombok

    → 主 Agent 直接调用文件工具
    → 在 memory/ 目录生成 / 更新 feedback_xxx.md 记忆文件
        - 写入 frontmatter + 内容
    → 更新 MEMORY.md 索引，增加一条指针
    → 更新 SnapshotTracker 快照记录（文件变更）

    特点：
        → 同步执行
        → 用户可以看到 "Writing memory" 提示


3.2 路径 2：后台自动提取（forked 后台子 Agent，不阻塞主对话）

    触发时机：
        → 每一轮对话结束
        → 主 Agent 一轮 LLM + 工具跑完

    执行流程：
        → 框架 fork 一个后台子 Agent（逻辑 fork，不是操作系统 fork）
        → 拿到本轮新增消息片段
        → 有游标标记上次提取位置，只处理新增内容，不重复处理历史

    后台 Agent 限制（安全沙箱）：
        → 禁止 bash
        → 禁止 MCP
        → 禁止再派子 Agent
        → 仅允许读写 memory 目录文件
        → 最多运行 5 轮
        → 尽力执行，失败静默，不干扰主会话

    判断逻辑：
        → 哪些信息属于：用户偏好、项目决策、反馈、踩坑经验
        → 值得持久保存

    去重：
        → 如果本轮主 Agent 已经手动写过记忆
        → 后台直接跳过，避免重复写入

    输出：
        → 更新 / 新建主题 md 文件
        → 更新 MEMORY.md 索引
        → 写入磁盘

    ⚠️ 这个后台子 Agent 和普通子 Agent 一样复用整套 Harness
        → 但是 task 是专门的记忆提取 prompt
        → 不阻塞主对话，用户可以继续聊天


【四、AutoDream（记忆整理，AI "做梦"）】
================================================================================

触发条件：
    → 距离上次 dream ≥ 24 小时
    → 并且累计 ≥ 5 次新会话

执行流程：
    → 后台 fork 专门 Dream 子 Agent
    → 读取全部现有记忆文件
    → 做：合并冲突记忆、删除过期、拆分过长文件
    → 清理 MEMORY.md 索引，防止索引超 200 行上限
    → 输出回写到 memory 目录

特点：
    → 静默运行，用户无感知
    → 属于记忆的 GC / 整理

作用：
    → 防止记忆越堆越多
    → 防止索引爆炸


【五、会话启动：记忆如何加载进 Agent】
================================================================================

步骤：

    1. 加载多层 CLAUDE.md
        → managed > user > project > local
        → 全部拼接进 system 动态块

    2. 读取项目 MEMORY.md
        → 最多读取前 200 行 / 25KB
        → 追加到 system 动态块

    ✅ 因为放在 system 动态块
        → 子 Agent 使用 model:inherit 时
        → 会继承这份 MEMORY.md 索引

    3. 各个主题记忆文件（feedback_xxx.md）不会读入 system
        → 模型看到索引之后
        → 需要的时候调用 read("memory/feedback_xxx.md")
        → 读取完整记忆


5.1 子 Agent 记忆继承

    → 子 Agent：继承父的 system（含 MEMORY.md 索引）
    → 但是不会自动加载父已经 read 过的主题文件
    → 子 Agent 需要自己调用 read 读取记忆主题文件

重点区分：
    MEMORY.md 索引：
        → 进 system
        → model:inherit 子 Agent 可以继承

    各个记忆主题文件：
        → 不进 system
        → 必须工具 read 读取
        → 子 Agent 不会自动拿到


【六、/memory 斜杠命令（记忆管理）】
================================================================================

和前面讲的 Skill 斜杠命令同一套解析机制：

    用户输入 /memory
        → 消息预处理识别 / 开头斜杠命令
        → 不是 skill，是框架内置斜杠命令

    执行逻辑：
        → 读取 memory 目录
        → 读取 MEMORY.md 索引
        → 整理输出当前全部记忆清单
        → 支持查看、编辑、删除记忆

    用户可以直接编辑磁盘上 memory 目录下 markdown 文件
        → 修改立即生效（新开会话生效）


【七、Memory 系统与其他模块关系】
================================================================================

7.1 和 SnapshotTracker 文件快照

    → 当 Agent 写 memory 目录下的 md 文件
    → 必须同步更新 SnapshotTracker 快照记录
    → 否则后续 edit 会误判 "文件被外部修改，需要重新 read"
    → 产生大量不必要重读


7.2 和 Hook

    → 写记忆文件（write/edit）会完整走 PreToolUse / PostToolUse 钩子
    → 可以配置 Hook 拦截禁止写记忆


7.3 和 MCP

    → Auto Memory 完全是本地文件
    → MCP 不能直接写入 memory 目录
    → MCP 要写记忆，只能通过调用本地内置 write 工具


7.4 和 Skill

    Skill：
        → 注入 messages 数组
        → 不会被 model:inherit 继承

    Memory 的 MEMORY.md 索引：
        → 注入 system 动态块
        → 会被 model:inherit 继承

    所以：
        → 子 Agent 天生能看到记忆索引
        → 但是要自己 read 拿详细记忆内容


7.5 和子 Agent（model:inherit）

    → system 复制过来，包含 MEMORY.md 索引
    → 子 Agent 知道有哪些记忆主题
    → 但是主题文件没有加载
    → 子 Agent 必须调用 read 读取
    → 子 Agent 也可以写记忆文件，直接操作磁盘 memory 目录
    → 多个 Agent 共享同一份磁盘记忆


【八、伪代码简化核心对象】
================================================================================

    // 记忆管理器
    class MemoryManager {
        memoryDir: string;

        // 会话启动读取索引，返回文本，注入system动态块
        async loadMemoryIndex(): Promise<string> {
            const raw = await fs.readFile(
                path.join(memoryDir, "MEMORY.md"),
                "utf-8"
            );
            // 截断200行/25KB
            return truncateMemoryIndex(raw);
        }

        // 创建/更新记忆主题文件
        async writeMemoryTopic(
            topicMeta: MemoryTopicMeta,
            content: string
        ): Promise<void> {
            // 写 xxx.md 主题文件
            // 更新 MEMORY.md 索引
            // 更新 SnapshotTracker 快照
        }

        // 触发后台自动提取记忆，fork后台子Agent，不阻塞主会话
        async triggerBackgroundExtraction(
            newMessages: Message[]
        ): Promise<void> {
            const backgroundAgent = forkBackgroundAgent();
            backgroundAgent.runMemoryExtractionTask(newMessages);
        }

        // AutoDream后台整理
        async triggerAutoDream(): Promise<void> {}
    }


【九、复刻这套记忆系统的工程要点（可以抄）】
================================================================================

1. 索引 + 主题文件分离
    → 索引小，每次会话加载进 system
    → 详细记忆大文件按需 read，避免 token 爆炸
    → 设置索引行 / 大小硬上限，强制截断

2. 两条写入路径
    → 用户显式直接写
    → 对话结束后后台非阻塞子 Agent 自动提取
    → 后台 Agent 做能力沙箱限制

3. 区分 system 和 messages
    → 索引放 system，子 Agent 可以 inherit 继承
    → 详细记忆不走 system，工具按需读取

4. 记忆文件带上 YAML frontmatter
    → 标记记忆类型
    → 方便模型分类管理

5. 提供斜杠命令查看 / 管理记忆
    → 允许用户直接修改磁盘 md 文件

6. 记忆目录写入后，同步更新 SnapshotTracker
    → 避免 edit 校验误判

7. 后台 Dream 任务做记忆合并、修剪
    → 防止索引无限膨胀


【十、容易踩坑的点】
================================================================================

⚠️  子 Agent 继承 MEMORY.md 索引
    → 但不会自动读取主题文件
    → 子需要自己 read
    → 很多人误以为子 Agent 拿到完整记忆

⚠️  修改磁盘 memory 目录文件
    → 当前正在跑的会话不会热加载
    → 新开会话才生效

⚠️  MEMORY.md 只是索引
    → 不要把大量内容直接写进 MEMORY.md
    → 会触发截断丢失

⚠️  后台记忆提取是尽力机制
    → 会失败静默
    → 不会报错给用户

================================================================================
```




## 上下文压缩
层级	      名称	实现逻辑	成本	触发时机
L1	      Tool‑Result Budget	bash/grep/read 大输出，超大内容落盘本地，消息只保留预览 + 文件路径，需要时再读回	0 成本	单次 tool_result 输出超标 (50000 字符)
L2	      Snip Compact	删除空消息、合并重复消息、裁剪低价值 trace 日志，保留 tool_use 结构，不删消息对象	0 成本	每次查询前，feature 开关控制
L3	      Microcompact 微压缩	清理旧 tool_result 内容，保留调用轨迹；时间超时自动清理；支持 cache_edits 服务端编辑缓存	0 成本	会话闲置、历史工具结果过多
L4	      Context Collapse 折叠	把一组已经完成的子任务，折叠成结构化快照摘要，保留引用 ID，不破坏工具调用链路	低，少量 LLM	多个子任务执行完毕
L5‑1	  AutoCompact 自动压缩	Fork 子 Agent 做结构化全量摘要，提炼目标、已完成、待办、技术决策；丢弃原始日志	消耗 token	token 达到窗口软阈值 83.5%
L5‑2	 Reactive Compact 应急压缩	兜底策略，API 返回 413 prompt_too_long 报错触发，激进裁剪历史	高成本	请求报错后 fallback




### 缺点
压缩会重写 mutableMessages 数组——这意味着压缩之后的所有 prompt cache 都会失效，下一次请求要重新支付 cache write 成本。这就是为什么 Autocompact 不能太激进：每次压缩都是一次"全量 cache 重建"，频繁压缩反而比放任上下文增长更贵。源码中 autocompact 的触发阈值据 ThreeFish-AI 分析约在 75-80% 上下文容量，留出余量避免过早压缩。

cache失效

### 核心
1. Autocompact 流程不是"丢一半旧消息"那么粗暴，而是一个三段式：
2. 优先规则裁剪，后 LLM 摘要：能删工具输出就不做摘要，摘要会产生额外 token 开销。

preservedSegment 是关键
设计——压缩后，最近 N 轮（社区估计 5-10 轮）保留原文，更早的内容被 LLM 摘要替换。这避免了"刚说过的事下一句就忘了"的体验。同时关键文件引用（“我们正在改 auth.py”）和 skill 状态（“已加载 pdf-processing skill”）被白名单恢复——压缩不能让 agent 失忆到不知道自己在干什么。

2. 压缩的熔断和重试



### 扩展
1. 需要给 用户提供 自定义压缩的，操作权限
```json
async def pre_compact_archive(input_data, context):
    # 压缩前把完整 transcript 归档到外部存储
    transcript = input_data["transcript"]
    await save_to_s3(f"sessions/{input_data['session_id']}.jsonl", transcript)
    # 注入一段 systemMessage 让模型知道"完整历史在 S3 上"
    return {"systemMessage": "Full transcript archived to S3, "
                              "can be referenced by session_id."}

```



## hook系统

每个 hook 通过 stdin 接收 JSON 输入（session_id / transcript_path / cwd / permission_mode / hook_event_name / tool_name / tool_input 等），通过 exit code 与 stdout JSON 决定后续行为。一个常见误区：






```text
================================================================================
                   Claude Code Hooks（钩子系统）完整原理
================================================================================


【核心定位】
================================================================================

在 Agent 循环的各个生命周期节点插入外部确定性逻辑。

关键区别：
    → 提示词（prompt）是概率性的，模型有可能忘记、忽略指令
    → Hook 是应用层强制执行，一定会跑
    → 用来做安全护栏、拦截、校验、自动化

注意：
    → 不是 LLM 内部能力
    → 是 Claude-Code CLI/SDK 的上层框架能力
    → Anthropic 原始 API 没有 hook


【一、整体执行流程（Agent 主循环插入点）】
================================================================================

                    SessionStart【钩子触发】
                         ↓
    用户输入 → UserPromptSubmit【钩子触发】 → LLM生成tool_use
                         ↓
    PreToolUse【钩子触发】 → 权限校验 → 真正执行工具(read/edit/bash)
                         ↓
    PostToolUse / PostToolUseFailure【钩子触发】
                         ↓
    Stop【钩子触发：任务是否允许结束】
                         ↓
                    SessionEnd【钩子触发】

子 Agent 有独立事件：
    → SubagentStart
    → SubagentStop


工作机制（命令行类型 hook 最典型）
-------------------------------------------------------------------------------

    1. Agent 运行走到某个生命周期事件
    2. 读取 settings.json / Skill frontmatter 里注册的 hook 配置
    3. 通过 matcher 匹配：匹配工具名（Bash/Edit/Write），过滤哪些场景才执行这个钩子
    4. 并行拉起外部处理器，把事件上下文 JSON 通过 stdin 传给外部脚本 / 程序
       （不用命令行传参，避免超长、转义问题）
    5. 等待 handler 返回：读取 exit_code、stdout、stderr，或者结构化 JSON 输出
    6. 根据返回结果做动作：放行、阻断本次操作、修改工具入参、注入上下文给模型、告警
    7. 回到 Agent 主循环继续跑

关键点：
    → 同步阻塞执行（除非标记 async）
    → PreToolUse 返回阻断，工具直接不会执行
    → 模型拿不到工具结果，强制拦截风险动作


【二、4 种 Hook 处理器类型（handler）】
================================================================================

2.1 command（shell 脚本，最常用）
    → 执行外部可执行脚本
    → stdin 输入 JSON 事件数据
    → 靠 exit code 做决策

2.2 http
    → POST JSON 事件 payload 到外部 http 接口
    → 拿响应做决策
    → 适合远程安全网关、审计

2.3 prompt
    → 内部调用 LLM 做语义判断
    → 例如判断 bash 命令是否高危

2.4 agent（子 Agent）
    → fork 子 Agent 执行复杂校验逻辑
    → 可以复用 model:inherit 缓存继承


【三、退出码契约（command 类型核心）】
================================================================================

┌─────────────┬────────────────────────────────────────────────────────────────┐
│  exit_code  │  行为                                                         │
├─────────────┼────────────────────────────────────────────────────────────────┤
│  0          │  正常放行；stdout 视事件决定是否注入上下文，stderr 给到 Agent  │
│             │  日志                                                         │
├─────────────┼────────────────────────────────────────────────────────────────┤
│  2          │  阻断操作；stderr 内容作为反馈喂给 Claude 模型                │
├─────────────┼────────────────────────────────────────────────────────────────┤
│  其他       │  告警，不阻断流程；stderr 输出日志                            │
└─────────────┴────────────────────────────────────────────────────────────────┘

示例：
    → PreToolUse 钩子检测到危险 bash 命令，exit 2
    → 工具直接不执行
    → stderr 的文字会反馈给模型


【四、配置来源（优先级从高到低）】
================================================================================

    1. Skill Frontmatter（单个 skill 内部定义，只在该 skill 生命周期生效）
    2. 项目级：.claude/settings.json
    3. 用户全局配置：~/.claude/settings.json
    4. 组织级配置

注意：
    → 子 Agent 会继承父 hook，但可以在 frontmatter 覆盖 / 关闭
    → disableAllHooks 可以临时关闭钩子系统


settings.json 极简示例
-------------------------------------------------------------------------------

{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "./.claude/hooks/shell-security-check.sh"
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": "./.claude/hooks/lint-after-edit.sh"
                    }
                ]
            }
        ]
    }
}


【五、关键事件列表（高频）】
================================================================================

SessionStart
    → 会话启动，初始化环境、检查依赖

UserPromptSubmit
    → 用户输入提交后，模型处理前
    → 拦截用户输入、注入上下文

PreToolUse（最重要）
    → 工具调用之前
    → 拦截高危 bash、保护敏感文件、审计

PostToolUse
    → 工具执行成功之后
    → 自动格式化、跑单元测试、变更校验

PostToolUseFailure
    → 工具执行失败之后

Stop
    → Agent 准备结束任务的时候
    → 做质量门禁，不通过就禁止结束，强制继续修复

SubagentStop
    → 子 Agent 任务结束，做子任务验收

SessionEnd
    → 会话销毁，清理、统计上报


【六、Hook 和其他组件的区别（很容易混淆）】
================================================================================

Hook vs CLAUDE.md 提示词
    CLAUDE.md：属于 system prompt，概率约束，模型可能忽略
    Hook：应用层强制执行，确定性，可以直接阻断动作
    工程范式：软约束写在 CLAUDE.md；硬安全护栏写 Hook

Hook vs Skill
    Skill：是给 Agent 调用的任务能力，Agent 主动选择调用
    Hook：生命周期事件自动触发，不管 Agent 愿不愿意，一定会跑

Hook vs Subagent(model:inherit)
    Subagent：派出去干活，执行业务任务，可以复用 prompt cache
    Hook：回调拦截、校验、门禁
    Hook 内部可以拉起 subagent 做复杂判断


【七、源码层面怎么实现（复刻思路，伪 TS）】
================================================================================

// agent主循环，每一轮
async function agentLoop() {
    while(true) {
        // 模型返回tool_use
        const toolCall = await llmGenerate();

        // ========= PreToolUse Hook 执行点 =========
        const hookResult = await executeHooks("PreToolUse", {
            toolName: toolCall.name,
            toolInput: toolCall.input,
            sessionId: this.sessionId
        });

        // hook返回阻断，直接跳过工具执行
        if(hookResult.blocked) {
            // 将hook的反馈消息推入messages，继续循环
            this.appendMessage(hookResult.feedback);
            continue;
        }

        // hook可以修改工具参数
        if(hookResult.updatedInput) {
            toolCall.input = hookResult.updatedInput;
        }

        // 真正执行工具
        const toolOutput = await runTool(toolCall);

        // ========= PostToolUse Hook =========
        await executeHooks("PostToolUse", {
            toolName,
            toolInput,
            toolOutput
        });

        // 把工具结果塞回对话
        this.appendToolResult(toolOutput);
    }
}

// executeHooks内部逻辑
async function executeHooks(eventName: string, payload: any) {
    // 1. 从配置取出当前event的所有hook
    // 2. matcher过滤，选出匹配当前工具的hook
    // 3. 并行执行各个handler(command/http/prompt/agent)
    // 4. 收集exit_code、stdout、stderr、结构化输出
    // 5. 合并决策：是否block、是否修改入参、是否注入上下文
}

注意：
    → hook 运行不占用 LLM 上下文 token，是框架侧的回调
    → 只有 hook 输出的 feedback 才会追加进 messages 数组


【八、工程上的坑】
================================================================================

⚠️  Hook 是同步阻塞
    → 钩子脚本卡死会把整个 Agent 卡住
    → 需要配置超时时间

⚠️  PreToolUse 的 matcher 不要写 .* 全匹配
    → 会对每一次 read/grep 都跑钩子
    → 性能爆炸

⚠️  command 类型脚本要注意可执行权限
    → windows/linux 差异

⚠️  hook 内部拉起子 Agent 时
    → 子 Agent 也会继承父的 hook
    → 注意防止递归死循环

⚠️  hook 不能替代 prompt
    → 复杂业务逻辑还是交给模型
    → hook 只做校验、拦截、后置自动化


【九、和前面上下文工程、model:inherit 的联动】
================================================================================

    → Hook 在 Agent 主循环的各个断点插入
    → Hook 内部可以 fork model:inherit 子 Agent
    → 复用父已经建好的 prompt-cache 做复杂校验
    → PreCompact 钩子：在五级压缩执行之前触发
    → 可以自定义上下文裁剪逻辑

================================================================================
```



## tool
1. 工具并行 只读工具的并行执行。Tool annotation 里的 readOnlyHint: True 让 harness 可以把 Read / Glob / Grep / WebFetch 一批工具调用并行起来；mutating 工具（Edit / Write / Bash）则严格串行，避免文件竞态。这是为什么 Claude Code 在"先大量探索再做修改"的任务上看起来很快——前半段被 harness 自动并行了，模型完全不感知。

```text
================================================================================
                   Claude-Code Tools（工具系统）完整实现解析
================================================================================


【核心定位】
================================================================================

Claude Code 的工具不是 Anthropic API 的工具那么简单。

Anthropic API：
    → 只负责返回 tool_use JSON
    → 不做任何实际执行

Claude-Code：
    → 自己实现一整套工具运行时
    → 工具注册、参数校验、安全拦截 (Hook)、沙箱
    → 输出处理、大输出截断 / 落盘、错误处理
    → 把结果包装成 tool_result 塞回对话

内置工具列表：
    → read、edit、write、bash、glob、grep
    → Agent（派生子 Agent）
    → BackgroundTaskStatus、BackgroundTaskCancel 等


【整体链路】
================================================================================

    LLM输出 → tool_use(JSON)
        ↓
    框架分发路由 → PreToolUse Hook（拦截/修改参数）
        ↓
    工具实现执行（文件操作 / shell / 派生子Agent）
        ↓
    捕获输出、捕获异常、超大输出做L1工具结果预算处理
        ↓
    PostToolUse / PostToolUseFailure Hook
        ↓
    包装成 tool_result，push进messages数组，交还给LLM


【一、工具的数据结构定义】
================================================================================

分为两层：

1.1 给 Anthropic 看的 JSON Schema
    → 传给 API 的 tools[]
    → 告诉模型 "有哪些工具、参数长啥样"

1.2 框架内部工具实现对象
    → ToolDefinition
    → 包含名字、schema、执行函数、权限标签、能力标记

伪 TS 定义：

    interface ToolDefinition {
        // 给模型看的，原样透传给 anthropic messages.create({tools})
        anthropicToolSchema: AnthropicTool;

        // 框架内部处理器
        name: string;
        tags: Set<"file" | "shell" | "agent" | "background">;

        // 是否允许子agent继承使用
        allowInSubagent: boolean;

        // 真正执行逻辑
        handler: (input: any, ctx: ToolUseContext) => Promise<ToolResult>;
    }


示例：bash 工具的简化定义

    const bashTool: ToolDefinition = {
        name: "bash",

        anthropicToolSchema: {
            name: "bash",
            description: "执行shell命令",
            input_schema: {
                type: "object",
                properties: { command: { type: "string" } },
                required: ["command"]
            }
        },

        tags: new Set(["shell"]),
        allowInSubagent: true,

        async handler(input, ctx) {
            // 真正执行shell，做工作目录隔离、超时控制
            return await runShell(input.command, ctx.cwd, ctx.timeout);
        }
    };

关键点：
    → Agent 本身也是一个普通工具！
    → 模型调用它，框架就 fork 子 Agent
    → BackgroundTaskStatus / BackgroundTaskCancel 也是工具
    → 专门处理异步后台子任务


【二、ToolUseContext 工具执行上下文（非常重要）】
================================================================================

每次执行工具，传入独立上下文对象，隔离主 / 子 Agent。

    interface ToolUseContext {
        sessionId: string;
        agentId: string;              // 区分是哪个agent（主/子agent）
        cwd: string;                 // 当前工作目录，子agent可以独立
        timeout: number;
        abortSignal: AbortSignal;     // 取消信号，父取消，子全部终止
        allowedTools: Set<string>;    // 当前agent允许运行哪些工具

        // 钩子、日志、安全配置
        hookRunner: HookRunner;
    }

关键理解：
    → 子 Agent fork 的时候会生成自己的 ToolUseContext
    → 可以裁剪 allowedTools
    → 比如禁止子 Agent 调用 Agent，防止无限递归 fork


【三、完整一次工具调用执行流程（伪代码）】
================================================================================

/**
 * 收到模型返回的tool_use
 */
async function processToolCall(toolUse: ToolUse, ctx: ToolUseContext) {
    const { name, input, id: toolCallId } = toolUse;

    // 1. 检查：当前agent是否被允许调用这个工具
    if(!ctx.allowedTools.has(name)) {
        return {
            type: "tool_result",
            tool_use_id: toolCallId,
            content: `error: tool ${name} is disabled`
        };
    }

    // ========== 【PreToolUse Hook】钩子执行点 ==========
    const hookPreResult = await ctx.hookRunner.runHooks("PreToolUse", {
        toolName: name,
        toolInput: input,
        ctx
    });

    // hook阻断：直接不执行工具，把hook反馈作为tool_result返回给模型
    if(hookPreResult.blocked) {
        return {
            type: "tool_result",
            tool_use_id: toolCallId,
            content: hookPreResult.feedback
        };
    }

    // hook可以修改工具入参
    if(hookPreResult.updatedInput) {
        input = hookPreResult.updatedInput;
    }

    // 2. 找到对应的工具handler
    const toolDef = getToolDefinition(name);
    let toolResultRaw;

    try {
        // 真正执行业务逻辑(read/edit/bash/Agent)
        toolResultRaw = await toolDef.handler(input, ctx);

    } catch (err) {
        // 工具执行异常
        await ctx.hookRunner.runHooks("PostToolUseFailure", {
            toolName: name,
            error: err
        });

        return {
            type: "tool_result",
            tool_use_id: toolCallId,
            content: `tool error: ${err.message}`
        };
    }

    // ========== PostToolUse 后置钩子 ==========
    await ctx.hookRunner.runHooks("PostToolUse", {
        toolName: name,
        input,
        output: toolResultRaw
    });

    // 3. 【L1工具结果预算】超大输出处理
    // bash/grep输出巨大时，不全部塞进上下文
    // 落盘本地，消息只保留预览+文件路径
    const processedOutput = await applyToolResultBudget(toolResultRaw);

    // 4. 包装成标准tool_result，追加进messages数组，返回给LLM
    return {
        type: "tool_result",
        tool_use_id: toolCallId,
        content: processedOutput
    };
}

重点：
    → 所有工具执行都发生在框架侧，不在模型内部
    → 模型只输出 JSON，它不知道命令有没有真跑成功


【四、几个关键内置工具内部实现特点】
================================================================================

4.1 read / edit / write 文件工具
    → 做路径沙箱：禁止跳出项目根目录，防止越权读取系统文件
    → edit：不是简单覆盖，做 diff 式编辑
        - 编辑失败返回原始文件 + 错误提示给模型
    → 大文件读取：做片段读取
        - 不会一次性读整个超大文件进上下文

4.2 bash 工具
    → 每个 Agent 有独立 cwd
    → 超时控制，防止卡死
    → PreToolUse 钩子最常用的目标：在这里拦截高危 shell
    → stdout/stderr 输出如果特别大，触发 L1 预算
        - 输出写入本地临时文件
        - 上下文中只返回预览 + 文件路径

4.3 Agent 工具（派生子 Agent）
    → 前面讲过，它就是一个普通 handler
    → handler 内部执行 forkSubAgent()

    如果 run_in_background: false：
        → await 子 agent 完整跑完，返回最终报告

    如果 run_in_background: true：
        → 立刻返回 taskId
        → 注册到 backgroundTasks，不等待子执行

4.4 BackgroundTaskStatus / BackgroundTaskCancel
    → 操作内存中那个 backgroundTasks Map
    → 做状态查询、abort 取消


【五、工具输出处理：L1 Tool-Result Budget（五级压缩第一层）】
================================================================================

这是 Claude Code 非常关键的设计：

问题：
    → bash、grep 很容易输出几十万 token
    → 如果全部塞到 messages，上下文瞬间爆炸

解决方案：
    1. 工具返回输出，如果超过阈值（例如 50000 字符）
    2. 把完整输出写入本地临时文件
    3. 给到模型的 tool_result 只返回简短预览
        → 附带提示："完整输出已保存到xxx，你可以用read读取"
    4. 模型需要看完整内容时，再调用 read 读这个临时文件

好处：
    → 不丢数据
    → 避免把巨量文本直接压进 LLM 上下文
    → 属于 0 成本规则级压缩
    → 不需要调用 LLM 做摘要


【六、工具、Hook、子 Agent、上下文压缩之间的关系】
================================================================================

Hook 是工具执行的拦截器：
    → PreToolUse 在工具跑之前，可以阻断、修改参数
    → PostToolUse 在跑完之后执行

Agent 工具产生子 Agent：
    → 子 Agent 拥有自己独立 ToolUseContext
    → 独立 allowedTools
    → 子 Agent 内部也会完整跑同一套工具执行、hook、五级压缩

工具输出是上下文膨胀最大来源：
    → L1 Tool-Result Budget 专门处理工具大输出
    → 是五级压缩流水线第一道关卡

model:inherit：
    → 只影响 system 提示词和 prompt-cache
    → 和工具本身无关
    → tools schema 每次 API 请求都完整传递，不会被缓存


【七、容易混淆的点】
================================================================================

❌ 不是 Anthropic 直接在服务器执行 bash/read
    → 所有文件、shell 全部在本地客户端（Claude-Code CLI 所在机器）执行
    → 只是把结果通过 tool_result 传回给模型

❌ 工具 schema（tools 数组）不会被 prompt cache 缓存
    → 即便你用 model:inherit 复用前缀 KV
    → tools 参数每次请求完整上传

❌ Hook 不是工具
    → Hook 是事件回调，可以拦截工具
    → 但本身不是给模型调用的工具

❌ 子 Agent 不是模型的能力
    → 是框架收到 Agent 工具调用之后
    → 拉起另一套完整 queryLoop


【八、复刻启示（自己做 Agent 框架参考）】
================================================================================

1. 把 "工具描述（给 LLM 的 schema）" 和 "工具执行 handler" 拆开

2. 每个工具调用要有独立上下文对象
    → 方便做子 Agent 权限隔离

3. 工具执行前后留出钩子事件点
    → 做安全校验

4. 一定要处理超大工具输出
    → 不要无脑全部塞进 messages
    → 支持落盘临时文件

5. 子 Agent 也是一个工具
    → 不要做成特殊硬编码逻辑

================================================================================
```


### 并行调用 文件快照 如何实现
```text
================================================================================
        Claude-Code：工具并行调用 + 文件快照校验 + Tool 与 Agent 整体关系
================================================================================

把三块合在一起讲，同时讲工程痛点与设计思路。


【一、Tool 并行调用：StreamingToolExecutor 调度器】
================================================================================

背景：
    → Claude API 支持一次返回多个 tool_use
    → 模型可以同时要求读多个文件、grep、bash

核心设计：
    → 不是无脑 Promise.all 全部并发
    → 框架给每个工具打上 isConcurrencySafe 标记
    → 做队列调度


1.1 标记规则

┌─────────────────────┬─────────────────────┬──────────────────────────────────┐
│      工具           │  isConcurrencySafe  │  说明                           │
├─────────────────────┼─────────────────────┼──────────────────────────────────┤
│ read、grep、glob    │  true               │  只读，无副作用                 │
│                     │                     │  可以安全并发跑                 │
├─────────────────────┼─────────────────────┼──────────────────────────────────┤
│ edit、write、bash、 │  false              │  会修改磁盘 / 状态              │
│ Agent               │                     │  不能乱并行，进串行队列排队     │
└─────────────────────┴─────────────────────┴──────────────────────────────────┘


1.2 调度逻辑

    → SSE 流式接收模型输出
    → 收到一个 tool_use 就立刻调度，不等全部收完，降低延迟

    安全工具（只读）：
        → 直接并发执行

    非安全工具（写文件、改状态）：
        → 进入 FIFO 队列
        → 等待前面工具全部完成再执行

    失败熔断：
        → 一组并行工具里任意一个报错 / 取消
        → 同批其它并行任务会被 AbortSignal 取消
        → 避免半完成状态

    ⚠️ 坑：
        → bash 进程已经写到磁盘
        → 取消信号杀不掉已经写完的 IO
        → 会出现状态不一致 bug，是官方已知 issue


1.3 伪代码简化

    interface ToolDefinition {
        isConcurrencySafe: boolean;
        handler: Function;
    }

    class StreamingToolExecutor {
        private readonly parallelTasks: Promise[] = [];
        private readonly serialQueue: Array<() => Promise> = [];

        async dispatch(toolUse, ctx) {
            const def = getToolDef(toolUse.name);

            if(def.isConcurrencySafe) {
                // 只读，直接并发
                const p = processSingleTool(toolUse, ctx);
                this.parallelTasks.push(p);

            } else {
                // 修改状态，进串行队列
                this.serialQueue.push(() => processSingleTool(toolUse, ctx));

                if(this.serialQueue.length === 1) {
                    this.drainSerialQueue();
                }
            }
        }

        async drainSerialQueue() {
            while(this.serialQueue.length > 0) {
                const task = this.serialQueue.shift()!;
                await task();
            }
        }
    }

关键点：
    → API 层面支持并行 tool_call
    → 但是 Harness 层做安全管控
    → 写操作强制串行，防止文件竞态覆盖


【二、文件快照追踪 SnapshotTracker】
================================================================================

模块：snapshot-tracker.ts
    → 内存维护一张 Map：路径 → 文件快照记录

业务逻辑：
    1. 模型调用 read 读取文件之后，框架记录这份快照
    2. 当模型想要 edit 修改这个文件前
    3. 框架先对比磁盘真实文件状态
    4. 如果磁盘已经和快照不一致（外部人改了、别的工具改了）
    5. 不允许直接拿旧的上下文中的内容去编辑
    6. 必须重新 read 拿到最新内容，再执行 edit


2.1 数据结构

    interface SnapshotEntry {
        path: string;
        contentHash: string;    // 文件sha256
        mtime: number;          // 文件修改时间
        size: number;
    }

    class SnapshotTracker {
        private snapshots = new Map<string, SnapshotEntry>();

        // read工具执行成功后，记录快照
        recordRead(path: string, content: string) {
            const stat = fs.statSync(path);
            this.snapshots.set(path, {
                path,
                contentHash: sha256(content),
                mtime: stat.mtimeMs,
                size: stat.size
            });
        }

        // edit/write执行前校验
        isFileModifiedOutside(path: string): boolean {
            const snap = this.snapshots.get(path);
            if(!snap) return true;  // 没有快照，认为已经变更

            const currentStat = fs.statSync(path);
            // mtime变了 或者 hash变了，说明磁盘文件被外部改动
            return snap.mtime !== currentStat.mtimeMs;
        }
    }


2.2 完整业务流程

    1. LLM 调用 read("a.ts")，读取磁盘，返回内容给模型
       → 框架调用 recordRead 存入快照

    2. 用户在编辑器手动修改了磁盘上 a.ts

    3. LLM 拿着旧的、已经过期的上下文里的 a.ts 内容，调用 edit，想要修改

    4. 框架在 edit handler 内部先调用 SnapshotTracker 校验
       → 发现磁盘 mtime 与快照不一致

    5. 拒绝直接执行 edit
       → 返回错误给模型：文件已经被外部修改，请重新 read 获取最新内容后再编辑

    6. 模型会重新调用 read，拿到最新磁盘内容，再做 edit

这是非常关键的防御机制：
    → 防止 AI 拿着内存里旧版本的文件
    → 覆盖掉磁盘上用户已经手动修改的代码


2.3 注意事项

    → SnapshotTracker 是会话内存级，不是磁盘备份
    → 磁盘备份回滚是另外一套 fileHistory.ts 文件历史模块
    → 两者不是同一个东西


2.4 坑

    ⚠️  harness 内部自己写文件（比如 memory 模块写 MEMORY.md）
        → 必须同步刷新 SnapshotTracker
        → 否则会误判 "外部修改"
        → 产生大量不必要的重读逻辑，是官方真实 issue

    ⚠️  多进程会话之间 SnapshotTracker 互不感知
        → 只保护单会话内部


【三、Tool 和 Claude-Code Agent（主 / 子 Agent）之间的完整关系】
================================================================================

一句话总结：

    Agent = 循环 Harness（queryLoop）
          + 上下文管理（system 分层、五级压缩、checkpoint）
          + Hook 运行器
          + Tool 执行运行时

    Tool = Agent 用来跟外部世界交互的执行单元

    Agent 本身也是一个普通 Tool，用来派生子 Agent


3.1 分层关系

    ┌─────────────────────────────────────────────────────────────────┐
    │  模型（Model）：大脑                                           │
    │  → 只输出思考 + tool_use JSON                                 │
    │  → 不碰本地文件、shell                                        │
    └─────────────────────────────────────────────────────────────────┘
                                ↓
    ┌─────────────────────────────────────────────────────────────────┐
    │  Agent Harness（queryLoop 主循环）：外壳                      │
    │  → 组装 system 数组（静态 / 动态分界线、prompt-cache）        │
    │  → 执行五级上下文压缩、checkpoint 断点                        │
    │  → 调用 HookRunner 触发 PreToolUse/PostToolUse 钩子           │
    │  → 调用 StreamingToolExecutor 工具调度器执行 tool             │
    │  → 把 tool_result 塞回 messages，循环                         │
    └─────────────────────────────────────────────────────────────────┘
                                ↓
    ┌─────────────────────────────────────────────────────────────────┐
    │  Tool 集合：手脚                                               │
    │  → 普通工具：read/edit/write/bash/grep/glob                   │
    │  → 特殊工具：Agent（派生子 Agent）                            │
    │  → BackgroundTaskStatus、BackgroundTaskCancel                 │
    │  → 每个工具拥有独立 handler                                   │
    │  → 接收 ToolUseContext（agentId、cwd、allowedTools、          │
    │    abortSignal）                                              │
    └─────────────────────────────────────────────────────────────────┘


3.2 主 Agent、子 Agent、Tool 之间交互

    → 主 Agent 调用 Agent 工具 → fork 子 Agent

    → 如果 model:"inherit"：
        - 字节复制父的静态 system 块
        - 复用 Anthropic prompt-cache

    → 子 Agent 拿到：
        - 一套独立的 queryLoop
        - 独立 messages 数组
        - 独立 SnapshotTracker
        - 独立 ToolUseContext

    → 子 Agent 拥有裁剪后的工具集合 allowedTools
        - 可以禁用 Agent 工具防止无限嵌套 fork

    → 子 Agent 内部，同样完整跑整套工具调度、快照校验、Hook、上下文压缩

    → 子 Agent 所有 tool 结果默认不会进入主 Agent 的 messages
        - 子结束后，把最终报告作为 tool_result 返回给主 Agent


3.3 同步子 Agent vs 后台异步子 Agent 与 Tool

    同步子 Agent：
        → Agent 工具 handler await 子 queryLoop 全部结束
        → 拿到报告再返回 tool_result
        → 主循环阻塞

    后台异步子 Agent（run_in_background: true）：
        → handler 立刻返回 taskId
        → 注册到内存 backgroundTasks Map
        → 子 Agent 在后台独立跑自己的循环、调用自己的工具
        → 主 Agent 靠 BackgroundTaskStatus 工具轮询读取结果


3.4 关键边界区分

    ❌ Tool 不是 Agent
        → Agent 是承载 Tool 运行的循环框架

    ❌ Agent 工具不是模型原生能力
        → 是 Harness 内部实现的一个普通工具 handler

    ✅ 每一个子 Agent 都拥有完整独立的 Tool 运行时、快照追踪、hook、上下文压缩
        → 不是共享主 Agent 的状态

    ✅ model:inherit 只影响 system 提示词与 prompt-cache
        → 不共享工具运行时
        → 不共享 SnapshotTracker
        → 不共享 messages


【四、把整套链路串起来（从模型输出到工具执行）】
================================================================================

    模型输出 → tool_use[]
        ↓
    Agent Harness queryLoop
        ↓
    StreamingToolExecutor（根据 isConcurrencySafe 做并行/串行调度）
        ↓
    PreToolUse Hook 钩子
        ↓
    如果是 edit/write：
        → 先走 SnapshotTracker 校验磁盘是否被外部修改
        → 过期则拒绝执行，返回提示给模型
        ↓
    工具 handler 执行
        → read/edit/bash / Agent 工具 fork 子 agent
        ↓
    L1 Tool-Result Budget 大输出落盘处理
        ↓
    PostToolUse / PostToolUseFailure Hook
        ↓
    包装 tool_result，push 进 messages
        ↓
    进入下一轮循环


【五、工程启示（复刻 Agent 框架可以抄的点）】
================================================================================

1. 工具一定要区分只读安全工具 / 状态修改工具
    → 修改磁盘的强制串行
    → 不要无脑全部并发

2. 实现一套文件快照校验
    → read 之后记录 hash/mtime
    → edit 前校验磁盘是否已经变更
    → 避免 AI 用过期内存状态覆盖用户本地修改

3. 子 Agent 不要做特殊硬编码逻辑
    → 把派生子 Agent 实现为一个普通工具 handler

4. 子 Agent 拥有完整独立的工具运行时、快照、上下文
    → 仅通过返回报告与主 Agent 交互
    → 隔离状态

5. Hook 放在工具执行前后
    → 做拦截、参数修改、安全校验

================================================================================
```




### mcp工具接入
一： mcp提供的工具如何让模型知道
MCP Tool Search（mer.vin）：harness 只在 system prompt 里注入 MCP 服务器的"目录"（每个服务器一段简短描述），具体工具 schema 在模型决定调用某个服务器时才按需加载。这跟 Skill 的三层渐进披露是同一思路——把"能做什么"和"怎么做"分到两个加载时机。



二：mcp和claude code接入：
```text
================================================================================
                   Claude-Code MCP（Model Context Protocol）完整实现原理
================================================================================


【核心定位】
================================================================================

MCP 底层是 JSON-RPC 2.0 的客户端-服务端协议。

Claude-Code 本身是 MCP Client；
同时它也可以作为 MCP Server，把自身内置工具暴露给外部程序调用。

核心机制：
    → MCP Server 运行在独立进程
    → 通过 transport（stdio/http）和 Claude-Code 通信
    → 框架内部做一层桥接
    → 把 MCP 的远端工具包装成 Claude-Code 内部的 ToolDefinition 对象
    → 直接接入原有工具调度、Hook、权限体系

关键理解：
    → MCP 不是模型能力
    → 是 Agent 运行时的插件总线
    → MCP 工具和内置 read/bash/edit 走同一套工具运行链路
    → PreToolUse / PostToolUse Hook、权限控制、工具并发调度全部生效


【整体架构】
================================================================================

┌─────────────────────────────────────────────────────────────────────────────┐
│  Claude-Code Host（Node进程）                                              │
│                                                                           │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐                        │
│   │MCP-Client1 │  │MCP-Client2 │  │ ...多客户端 │  每个MCP Server对应    │
│   └─────┬──────┘  └─────┬──────┘  └──────┬─────┘  一个独立Client实例    │
│         │               │                │                                │
│    transport层（stdio / http / ws）                                       │
└─────────┼───────────────┼────────────────┼────────────────────────────────┘
          │               │                │
┌─────────▼───────────────▼────────────────▼────────────────────────────────┐
│  MCP Server进程（独立子进程 / 远程服务）                                  │
│  对外暴露 tools / resources / prompts                                    │
└───────────────────────────────────────────────────────────────────────────┘

传输方式：
    → stdio：Claude-Code child_process.spawn 拉起 MCP Server 子进程
        - 通过 stdin/stdout 管道双向收发 JSON-RPC 消息
        - 最常用本地插件模式

    → HTTP（StreamableHTTP）：远程 MCP 服务
        - 走 http 长连接双向流


【一、会话启动完整时序（SessionStart）】
================================================================================

1.1 加载 MCP 配置，三级合并
    → 用户全局：~/.claude/mcp.json
    → 项目级：./.claude/mcp.json
    → Skill/settings.json 内的 mcpServers 配置
    → 命令行 claude mcp add 写入配置文件

1.2 对每一个配置的 MCP Server，创建一个独立 McpClient 实例
    → stdio：spawn 子进程，监听进程退出，自动重连
    → http：建立 http 流连接

1.3 MCP 握手 initialize
    → 发送初始化 JSON-RPC
    → 协商协议版本、能力

1.4 调用 tools/list
    → 运行时动态拉取 MCP Server 暴露的全部工具列表
    → 包括：name / description / inputSchema

1.5 桥接包装：MCP 工具转为 Claude-Code 内部 ToolDefinition
    → 源码模块：src/tools/MCPTool/MCPTool.ts
    → 把远端 MCP 工具包装成本地 ToolDefinition 对象
    → 注入全局工具集合


伪代码：MCP工具包装

    function wrapMcpAsLocalTool(mcpClient, mcpToolInfo): ToolDefinition {
        return {
            name: `${mcpClient.serverName}__${mcpToolInfo.name}`,

            anthropicToolSchema: {
                name: mcpToolInfo.name,
                description: mcpToolInfo.description,
                input_schema: mcpToolInfo.inputSchema
            },

            tags: new Set(["mcp"]),
            isConcurrencySafe: true,    // 默认只读，可配置
            allowInSubagent: true,

            async handler(input, ctx) {
                // handler内部：向远端MCP Server发送 JSON-RPC tools/call
                const resp = await mcpClient.callTool({
                    name: mcpToolInfo.name,
                    arguments: input
                });
                return resp.content;
            }
        };
    }

1.6 所有包装完成的 MCP 工具
    → 和内置 read/bash/Agent 工具合并
    → 传给 StreamingToolExecutor 调度器

1.7 如果 MCP 工具总 schema 体积过大
    → 开启 Tool Search 延迟加载
    → 避免占用大量上下文窗口空间

⚠️ 重点：
    → MCP 工具在会话启动阶段动态发现
    → 运行时修改 mcp.json 不会热加载
    → 必须重启会话


【二、MCP 工具调用完整链路（和原生工具完全打通）】
================================================================================

    模型输出 tool_use（MCP工具）
        ↓
    StreamingToolExecutor 调度
        → 根据 isConcurrencySafe 做并行 / 串行
        ↓
    PreToolUse Hook
        → MCP 工具同样会触发钩子
        → 可以拦截 MCP 调用
        ↓
    MCP 包装后的本地 handler 执行
        ↓
    McpClient 发送 JSON-RPC "tools/call" 请求
        → 通过 transport 发给远端 MCP Server 进程
        ↓
    MCP Server 执行业务逻辑
        → 返回 JSON-RPC 响应
        ↓
    拿回结果
        → L1 Tool-Result Budget 超大输出处理
        ↓
    PostToolUse Hook
        ↓
    包装 tool_result，push 进 messages 数组
        → 回到 Agent 主循环


关键点：

    Hook 完全生效：
        → PreToolUse 可以拦截 MCP 调用
        → 可以修改参数、阻断外部 MCP 工具执行
        → 做安全管控

    子 Agent 同样可以使用 MCP 工具：
        → fork 子 Agent 时，MCP 工具集合会继承
        → 可以通过 allowedTools 裁剪禁用 MCP

    SnapshotTracker 文件快照：
        → 只对本地文件工具生效
        → MCP Server 自己的文件读写不受这个快照保护
        → MCP Server 有自己独立的文件访问权限
        → 这是安全边界

    MCP 的 resources 资源：
        → 不是 tool 调用
        → 框架可以把 resource 资源预取注入 system 动态块
        → __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__ 之后
        → 把外部文档、数据库内容喂给模型


【三、McpClient 内部核心能力】
================================================================================

    interface McpClient {
        serverId: string;
        transport: Transport;              // Stdio / StreamableHTTP
        status: "pending" | "connected" | "error" | "disconnected";

        // 握手
        initialize(): Promise<ServerCapabilities>;

        // 获取工具列表
        listTools(): Promise<McpTool[]>;

        // 执行远端工具
        callTool(name: string, args: Record<string, any>): Promise<McpToolResult>;

        // 资源读取
        listResources(): Promise<Resource[]>;
        readResource(uri: string): Promise<ResourceContent>;

        close(): Promise<void>;            // 关闭子进程/断开http连接
    }

设计要点：
    → 每个 MCP Server 一个独立 Client 实例，互相隔离
    → 一个挂掉不影响其他 MCP 服务

    → stdio 模式：
        - 监听子进程 exit 事件
        - 支持自动重连、超时控制

    → JSON-RPC 多路复用：
        - 同一个连接同时并发多个工具调用
        - 靠 id 匹配请求响应
        - 不需要每调用一次新建进程


【四、权限确认机制（MCP 安全设计）】
================================================================================

    → MCP Server 拥有独立权限（访问数据库、浏览器、外部 API）

    → Claude-Code 默认每次 MCP 工具调用弹出确认弹窗
        - 防止提示注入攻击
        - 模型不能绕过用户确认直接调用 MCP 服务

    → 可以在配置里配置 permissions
        - 对特定 MCP 工具自动放行，免确认

    → 内置 bash/read/edit 属于本地工具
        - 权限逻辑和 MCP 工具是两套


【五、MCP 与前面整套组件的关系】
================================================================================

5.1 与 System 分层
    → MCP 返回的 resources 资源
        - 放到分界线 __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__ 之后的动态块
        - 不会进入静态 cache 块
    → MCP 工具 schema 每次 API 请求完整上传
        - 不会被 prompt-cache 缓存

5.2 与 Tool 运行时
    → MCP 只是把远端能力包装成本地 ToolDefinition
    → 复用 StreamingToolExecutor 调度、Hook 流水线
    → 复用 L1 工具结果预算输出处理
    → MCP 工具和内置工具没有特殊代码分支

5.3 与子 Agent（model:inherit）
    → fork 子 Agent 时，MCP 工具集合会继承
    → model:inherit 只复用 system 静态块
    → MCP 工具列表每次 API 完整上传，不参与缓存继承
    → 子 Agent 可以通过 allowedTools 禁用全部 MCP 工具

5.4 与 Hook
    → MCP 工具完全参与 Hook 生命周期
    → PreToolUse 可以拦截 MCP 调用
    → 这是 MCP 最主要安全护栏

5.5 与五级上下文压缩
    → MCP 工具返回的巨大输出，同样走 L1 Tool-Result Budget
    → 超大输出落盘临时文件
    → 不全部塞进 messages


【六、自己复刻接入 MCP 到 Agent 框架的关键步骤（工程复刻）】
================================================================================

1. 实现 MCP Client 层
    → 实现 stdio/http transport
    → 封装 JSON-RPC 2.0 收发
    → 多路复用 id 匹配、超时
    → 进程生命周期管理

2. 会话启动阶段
    → 连接所有 MCP Server，握手
    → tools/list 拉取远端工具元数据

3. 适配桥接层
    → 把 MCP 工具转为本地 ToolDefinition 对象
    → handler 内部调用远端 tools/call

4. 将包装后的 MCP 工具
    → 合并进 Agent 工具集合
    → 交给原有调度器

5. 保证 MCP 工具完整走 PreToolUse/PostToolUse Hook 流水线

6. 处理 MCP resources 资源
    → 注入 system 动态块

7. 子 Agent fork 逻辑
    → 支持继承 / 裁剪 MCP 工具


【七、常见坑】
================================================================================

⚠️  MCP Server 崩溃退出
    → Claude-Code 要做重连逻辑
    → 否则 MCP 工具全部不可用

⚠️  stdio 传输时
    → Server 不能随便打印普通 console.log
    → 会污染 JSON-RPC 流，导致消息解析失败

⚠️  MCP 工具 schema 体积爆炸
    → 会把传给 Anthropic API 的 tools 数组撑大
    → 需要做 Tool Search 延迟加载

⚠️  SnapshotTracker 只保护 Claude-Code 本身的 read/edit
    → MCP Server 自己读写文件不受快照校验
    → MCP 有独立权限域

⚠️  修改 mcp 配置文件不会热生效
    → 必须重启会话


【八、MCP 与内置工具对比】
================================================================================

┌────────────────────────┬─────────────────────────────┬──────────────────────────────┐
│        项目            │  Claude-Code 内置工具        │  MCP 远端工具                │
│                        │  （read/bash）              │                              │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ 实现位置               │  Claude-Code 进程内部        │  独立子进程 / 远程服务       │
│                        │  handler                    │                              │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ ToolDefinition         │  代码硬编码                  │  运行时从 Server 拉取包装    │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ Hook                   │  ✅ 支持                    │  ✅ 完整支持 Pre/PostToolUse │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ 并发调度               │  StreamingToolExecutor      │  同样调度，可配置            │
│                        │  统一调度                   │  isConcurrencySafe 标记      │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ SnapshotTracker        │  ✅ 生效                    │  ❌ MCP Server 内部读写      │
│ 文件快照校验           │                             │  不受保护                    │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ prompt-cache           │  tools 数组不参与缓存       │  tools 数组不参与缓存        │
├────────────────────────┼─────────────────────────────┼──────────────────────────────┤
│ 子 Agent 继承          │  ✅ 可裁剪 allowedTools     │  ✅ 可继承，可裁剪禁用       │
└────────────────────────┴─────────────────────────────┴──────────────────────────────┘

================================================================================
```


三： claude code 如何区分是mcp的tool还是内置的tool

```text
================================================================================
              Claude-Code 源码分析关键点解读与总结
================================================================================

来源：magicliang 的 Claude-Code 深度解析
两个非常核心的工程设计点：MCP 工具命名空间设计、工具 handler 异常契约。


【一、工具命名：mcp__{server}__{tool}】
================================================================================

示例：mcp__weather__get_temperature


1.1 为什么要加这个前缀

    ✅ 全局唯一命名空间，避免名字冲突

    背景：
        → 内置工具：read / bash / Agent
        → MCP 服务是外部插件
        → 不同 MCP Server 完全可能出现同名工具

    问题场景：
        → 两个 MCP 服务都叫 query_db
        → 如果直接把工具名就叫 query_db
        → 会发生覆盖冲突

    解决方案：
        → 加上 mcp__server名__原始工具名
        → 全局名字唯一，不会撞名


1.2 Hook Matcher 可以用正则一次性匹配全部 MCP 工具

    正则表达式：
        ^mcp__

    PreToolUse 钩子 matcher 写这个正则：
        → 所有 MCP 来源的工具全部命中
        → 不需要枚举每一个 MCP 工具名

    配置示例：
        "matcher": "^mcp__"

    匹配效果：
        → 内置工具：bash、read → 不匹配
        → 所有 MCP 工具：mcp__xxx__yyy → 全部命中


1.3 设计思想

    内置工具、MCP 插件工具走同一个工具命名空间：
        → 同一套 ToolDefinition
        → 同一套调度器
        → 同一套 Hook

    MCP 工具不是一套独立分支逻辑：
        → 仅仅是名字带上前缀做区分
        → 其余全部复用原有整套工具运行时


1.4 小细节

    传给模型的 anthropicToolSchema 里面：
        → 对外展示的工具名，有的版本会暴露原始名字

    框架内部路由、hook 匹配、allowedTools 权限过滤：
        → 一律使用带 mcp__ 完整全名

    所以写 allowedTools 禁用 MCP 工具：
        → 要写完整名字
        → 或者用正则匹配


1.5 优势总结

    ✅ 解决多 MCP Server 之间工具重名冲突

    ✅ Hook 可以一条正则批量拦截全部 MCP 外部插件
        → 做统一安全审计

    ✅ 权限配置 allowedTools 可以批量开关 MCP

    ✅ 不需要给 MCP 做一套独立的 hook / 权限代码
        → 完全复用现有基础设施


【二、工具 handler 异常契约：禁止 throw】
================================================================================

核心原则：
    handler 抛未捕获异常会导致整个 agent loop 崩溃
    但如果 catch 后返回 {"is_error": True, ...}
    Claude 会把错误当数据，可以重试或换路径

生产代码铁律：
    必须永远 catch-and-return
    永远不要 throw


2.1 两种错误行为对比

❌ 错误写法：handler 内部直接 throw

    async function badHandler(input, ctx) {
        // 直接抛出异常
        throw new Error("连接MCP服务失败！");
    }

    后果：
        → 异常向上冒泡，跳出 processSingleTool
        → 如果没有外层兜底，整个 queryLoop 主循环直接崩溃退出
        → Agent 直接死掉，会话结束
        → 模型没有机会感知、重试、补救


✅ Claude-Code 标准契约：捕获异常，包装成业务错误返回

    async function goodHandler(input, ctx) {
        try {
            return await doRealWork(input);
        } catch(err) {
            // 不 throw！返回带 is_error 标记的结构化结果
            return {
                is_error: true,
                error_message: err.message,
                content: `工具执行失败：${err.message}`
            };
        }
    }

    效果：
        → 框架拿到这个返回
        → 把它包装成正常 tool_result 塞入 messages 给到模型
        → 对 Node.js 进程来说：没有抛出未捕获异常
        → agent 循环继续跑，不会崩

    对 LLM 来说：
        → 收到一份 "工具执行出错" 的数据
        → 模型可以：
            - 重试调用这个工具
            - 修改参数换一种方式调用
            - 换别的工具
            - 告诉用户发生什么问题


2.2 关键区分两个概念

    ┌─────────────────────────────────────────────────────────────────────┐
    │  业务层工具错误（预期失败）                                       │
    │  → 网络超时、文件不存在、MCP 调用失败                            │
    │  → catch 住，返回 is_error:true                                  │
    │  → 交给模型处理，Agent 继续运行                                  │
    └─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────┐
    │  框架致命异常（真正的 bug）                                      │
    │  → 空指针、框架内部逻辑 bug                                      │
    │  → 这种才允许向上抛出，终止会话                                  │
    └─────────────────────────────────────────────────────────────────────┘

工程铁律：
    所有 tool handler，业务侧异常一律捕获
    转为带 is_error 的返回对象，不要 throw
    throw 留给框架本身不可恢复的内部错误


2.3 延伸：和 Hook、子 Agent 的关系

    Hook 执行的时候，同样遵守这个契约：
        → Hook 处理器内部不能随便 throw
        → 异常要包装为 blocked 反馈
        → 返回给模型，而不是炸掉整个循环

    子 Agent 内部的工具 handler 同样遵守这套契约：
        → 子工具出错不会把子 Agent 的 queryLoop 搞崩
        → 子 Agent 继续跑
        → 把错误报告写到自己 messages

    只有 AbortController 取消（用户主动终止会话）：
        → 属于特殊信号
        → 允许向上抛出终止循环


【三、把两点和前面整套体系串起来】
================================================================================

    MCP 工具通过命名前缀 mcp__server__tool
        → 融入全局工具命名空间

    Hook 依靠 ^mcp__ 正则
        → 批量拦截所有外部 MCP 插件
        → 做安全校验

    不管是内置工具还是 MCP 工具
        → handler 都要遵守错误契约
        → 业务异常不抛异常，返回 is_error:true
        → 保证 queryLoop 不会因为单次工具调用直接崩溃

    全部复用同一套：
        → 调度器
        → Hook 流水线
        → L1 大输出处理
        → 子 Agent 权限过滤

这也是为什么 Claude-Code 可以大量接入第三方 MCP 插件
    → 同时稳定性可控


【四、工程范式总结（值得抄的）】
================================================================================

如果你要自己写 Agent 框架，这两条是非常值得抄的工程范式：

    1. 外部插件工具增加命名前缀隔离
        → 支持正则批量 hook 拦截

    2. 工具处理器严格 catch 业务异常
        → 把错误转为模型可见的数据
        → 而不是直接崩溃 Agent 循环

================================================================================
```



## skill

一： skill 如何 被claude code加载的
二： skill 如何 别claud code调用的： 手动 还是 自动
```text
================================================================================
                   Claude-Code Skill 完整原理与组件关系
================================================================================


【核心定位】
================================================================================

Skill 本质：
    → 封装一套任务 SOP / 领域知识的手册包
    → 懒加载
    → 不是工具，不是 hook，是提示词指令包

类比：
    Tool = 手脚，做动作
    MCP = 外接硬件
    Hook = 事件拦截器
    Skill = 培训手册，告诉模型 "这件事该怎么一步步做"

关键特性：懒加载
    → 会话启动只读取每个 Skill 顶部 YAML frontmatter 元数据
    → 不读取完整 markdown 正文
    → 只有被触发，才把完整 SKILL.md 内容注入对话消息
    → 避免几百个 Skill 把 token 直接打爆


【一、目录结构 & 存储位置、优先级（继承覆盖）】
================================================================================

每个 Skill 是独立文件夹，文件夹名字就是 skill 的 slug（斜杠命令名字 /xxx），
内部必须有 SKILL.md 入口文件。

    skill-demo/
    └── SKILL.md     # 入口，顶部 --- YAML frontmatter ---，下面是指令正文


1.1 四级存储位置，优先级从高到低

    1. Managed 企业托管
        → 管理员全局下发，优先级最高

    2. User 用户全局
        → ~/.claude/skills/xxx/SKILL.md
        → 本机所有项目生效

    3. Project 项目级
        → ./.claude/skills/xxx/SKILL.md
        → git 可提交，团队共享
        → 会覆盖用户全局同名 skill

    4. Plugin 插件内 Skill
        → 带命名空间 plugin:skillname
        → 不会和上面冲突

注意：
    → 会话启动时一次性扫描全部目录
    → 修改 SKILL.md 不会热重载，必须重启会话


1.2 SKILL.md 格式示例

    ---
    name: code-review
    description: 执行完整代码评审，找bug、安全问题、代码规范
    invoke: auto          # auto：模型自动匹配触发；manual：只能手动 /code-review 调用
    tags: ["code", "review"]
    ---
    # 代码评审SOP
    1. 先glob读取变更文件
    2. 逐文件read读取源码
    3. 检查安全漏洞、边界条件、异常处理
    4. 输出结构化评审报告

invoke 字段含义：
    → auto：模型看 description 判断当前上下文匹配，自动加载 skill 内容
    → manual：只能用户手动输入 /code-review 斜杠命令触发


【二、Skill 加载完整流程（会话启动 → 触发注入）】
================================================================================

2.1 阶段 1：会话初始化（只扫元数据，懒加载）

    → 扫描 4 个路径下全部 skill 文件夹
    → 解析每个 SKILL.md 头部 YAML frontmatter
        - 只解析元数据，不读大段 markdown 正文
    → 在内存维护一张 SkillRegistry 注册表
        - skillSlug → SkillMeta
        - 只存 name、description、invoke、tags
    → 不会把 skill 内容塞进 system prompt
        - 此时几乎不消耗 token

👉 这就是懒加载核心：
    → 几百个 skill 启动也很快
    → 只有元数据在内存


2.2 阶段 2：触发 Skill（两种触发方式）

A) 手动触发：用户输入 /code-review

    → Harness 识别斜杠命令，去注册表找到对应 skill
    → 磁盘完整读取 SKILL.md 全部正文
    → 包装为一条 user 消息，push 进当前 agent 的 messages 数组
    → 模型拿到完整 SOP，按 skill 里写的流程执行


B) 自动触发 invoke:auto

    → 用户发消息，模型判断当前任务匹配某个 skill 的 description
    → 模型调用内置 SkillTool 工具，传入 skill 名字
    → 框架收到 SkillTool 调用，磁盘读取完整 SKILL.md
    → 把 skill 全部指令注入 messages
    → 返回 tool_result
    → 模型下一轮就拥有这套 SOP 知识

⚠️ 重要：
    → Skill 内容注入到 messages
    → 不是写到 system 静态块
    → 不会进入 model:inherit 可复用的静态 prompt-cache 部分
    → 子 Agent fork 的时候，默认不会自动继承父 Agent 已经加载过的 skill 内容


【三、Skill 如何继承到子 Agent（重点）】
================================================================================

很多人踩坑：
    → 父 Agent 跑了 skill，子 Agent 默认看不到 skill 的指令
    → 因为 skill 是存在父的 messages 数组，不是 system


3.1 三种方案把子 Skill 能力给到子 Agent

方案 1：子 Agent 调用 skill 名字（子自己触发）

    → 子 Agent 内部同样完整拥有 SkillRegistry 注册表
    → 子 Agent 可以自己调用 SkillTool
    → 或者子内部执行 /xxx
    → 子自己加载 SKILL.md

    ✅ 所有 skill 对所有子 Agent 都可见
    ✅ 子 Agent 可以独立 auto/manual 触发
    ❗ 子 Agent 的 messages 独立，父加载的 skill 不会自动同步给子


方案 2：在 Agent 工具调用参数，直接把 skill 完整文本作为 task 传给子 Agent

    → 主 Agent 读取 skill 全部内容
    → 把 SOP 文本直接塞进子 Agent 的 task 参数

    {
        "name": "Agent",
        "input": {
            "task": "【这里粘贴skill完整SOP】请按照下面流程做代码评审......",
            "model": "inherit"
        }
    }


方案 3：skill frontmatter 指定 scope

    → 原版没有 "自动继承父已加载 skill" 机制

    如果希望子 Agent 默认自带某个 skill，两种工程做法：
        1. 子 Agent 启动阶段，框架主动调用 skill 加载逻辑
            → 把子的 messages 预先注入 skill 内容

        2. 把 skill 的规则写进 system 动态块（不是 messages）
            → 这样子 Agent 通过 model:inherit 复制 system 动态部分
            → 子天生拥有这套规则


3.2 区分：

    CLAUDE.md：
        → 全局 system
        → 子 Agent 通过 model:inherit 可以继承

    Skill：
        → 默认注入 messages
        → 不会被 inherit 继承


【四、Skill 与其它核心组件之间关系】
================================================================================

4.1 Skill vs Tool

    Tool：能做什么（原子执行动作 read/bash/mcp__xxx）
    Skill：该怎么做（流程 SOP）

    → Skill 不会新增工具
    → 只是给模型一套使用工具的步骤
    → Skill 内部可以写：你要调用哪些 MCP 工具、调用顺序、参数格式
    → 但本身不会注册新 tool


4.2 Skill vs Hook

    Hook：事件回调，工具执行前后触发执行逻辑
    Skill：是提示词指令，给模型看的文本

    → Skill 的 SKILL.md 里不能直接写 hook
    → 但是 skill 可以指示模型 "在合适时机调用某个 hook 相关逻辑"
    → 真正 hook 定义写在 settings.json


4.3 Skill vs MCP

    MCP：提供外部系统能力（查数据库、浏览器）
    Skill：告诉模型怎么正确调用 MCP，参数怎么填、解析返回结果的格式

    典型组合：
        → MCP 提供 postgres 查询能力
        → skill 写一套数据库分析 SOP
        → 告诉模型怎么写查询、怎么解读结果


4.4 Skill 与子 Agent、model:inherit

    model:inherit 只复制 system 数组（静态块 + 动态块）
    Skill 加载出来是 user 消息，放在 messages[]，不在 system

    👉 所以：父 Agent 加载 skill，子 Agent 用 inherit 也拿不到 skill 内容

    如果想让子 Agent 天然拥有 skill：
        → 要把 skill 内容放到 system 动态块
        → 而不是走 skill 运行时注入 messages


4.5 Skill 的生命周期

    → 每个 Agent（主、子）各自独立维护自己的 messages
    → skill 注入的内容只属于当前 agent
    → 子 Agent 不会自动复制
    → 子 Agent 内部拥有完整 SkillRegistry，子可以自己触发 skill
    → 后台异步子 Agent 同样可以使用 skill


【五、伪代码简化 Skill 核心对象】
================================================================================

    // 内存注册表，会话启动构建
    interface SkillMeta {
        slug: string;
        name: string;
        description: string;
        invoke: "auto" | "manual";
        tags: string[];
        filePath: string;
    }

    // 触发后完整加载后的skill
    interface Skill {
        meta: SkillMeta;
        fullContent: string;         // SKILL.md完整markdown
    }

    // SkillTool 工具处理器（模型调用这个工具触发skill）
    async function skillToolHandler(input: { skillSlug: string }, ctx: ToolUseContext) {
        const meta = skillRegistry.get(input.skillSlug);

        if(!meta) {
            return { is_error: true, content: "skill不存在" };
        }

        // 磁盘读取完整SKILL.md
        const fullContent = fs.readFileSync(
            path.join(meta.filePath, "SKILL.md"),
            "utf-8"
        );

        // 注入当前agent的messages
        ctx.messages.push({ role: "user", content: fullContent });

        return { content: `skill ${input.skillSlug}已加载完成` };
    }


【六、工程复刻要点（自己做 Agent 框架抄这一套）】
================================================================================

1. 元数据懒加载
    → 启动只解析 frontmatter，不读完整正文
    → 降低启动 token 开销

2. 多级目录优先级覆盖
    → 企业 > 用户全局 > 项目

3. 两套触发入口
    → 手动斜杠命令 + 模型调用 SkillTool 自动触发

4. 区分：skill 注入 messages，不是 system
    → 所以 inherit 不会自动继承 skill

5. 子 Agent 可以访问全局 SkillRegistry
    → 子可以独立加载 skill

6. Skill 只输出指令文本
    → 不新增工具、不注册 hook
    → skill 是 "手册"，不是执行代码


【七、一张表区分容易混淆的扩展单元】
================================================================================

┌────────────────────┬───────────────────────────┬──────────────────────────────┐
│      组件          │  本质                     │  是否被 model:inherit 继承   │
├────────────────────┼───────────────────────────┼──────────────────────────────┤
│  CLAUDE.md         │  全局系统提示词           │  ✅ 继承                    │
│                    │  system 动态块            │                              │
├────────────────────┼───────────────────────────┼──────────────────────────────┤
│  Skill             │  任务 SOP 手册            │  ❌ 不继承                  │
│                    │  messages 消息数组        │  子需要自己加载             │
├────────────────────┼───────────────────────────┼──────────────────────────────┤
│  Hook              │  事件回调处理器           │  子 Agent 默认继承 hook 配置│
│                    │  框架配置 settings.json   │                              │
├────────────────────┼───────────────────────────┼──────────────────────────────┤
│  MCP               │  远端工具                 │  子 Agent 继承 MCP 工具集合  │
│                    │  ToolDefinition 集合      │                              │
├────────────────────┼───────────────────────────┼──────────────────────────────┤
│  Tool              │  原子执行能力             │  子 Agent 通过               │
│                    │  ToolDefinition 集合      │  allowedTools 裁剪          │
└────────────────────┴───────────────────────────┴──────────────────────────────┘


【小总结】
================================================================================

    Skill 就是按需加载的大段提示词手册
    懒加载元数据是最大亮点
    它不会自动跟着 model:inherit 传给子 Agent

    子 Agent 获得 skill 的三种方式：
        1. 自己触发 skill
        2. 把 skill 文本直接塞进 task 参数
        3. 把 skill 内容放到 system 动态块

================================================================================
```


三： 主动触发 / skill-name 如何识别解析的
```text
================================================================================
              Claude-Code 斜杠命令 /skillname 完整解析逻辑
================================================================================


【核心定位】
================================================================================

核心：
    → 没有用第三方命令解析库
    → 全部是框架内部手写的轻量解析逻辑

设计基础：
    → 会话启动已经把全部 skill 元数据加载进内存 SkillRegistry
    → 斜杠命令就是输入文本的前缀匹配


【一、内存注册表 SkillRegistry】
================================================================================

会话初始化阶段：
    → 扫描 4 个 skill 目录
    → 解析每个 SKILL.md 头部 YAML frontmatter
    → 只存元数据，不读正文

数据结构：

    type SkillMeta = {
        slug: string;           // 就是斜杠后面的名字，例如 code-review
        name: string;
        description: string;
        invoke: "auto" | "manual";
        filePath: string;
    };

    class SkillRegistry {
        // key: skill slug，对应 / 后面的名字
        private index = new Map<string, SkillMeta>();

        // 会话启动扫描全部skill文件夹，填充index
        loadAllSkills(): void { ... }

        // 根据斜杠名字查找
        getBySlug(slug: string): SkillMeta | undefined {
            return this.index.get(slug);
        }

        // 返回全部skill列表，用于前端自动补全提示
        //（IDE/CLI输入框提示可用的 /xxx）
        listAll(): SkillMeta[] {
            return Array.from(this.index.values());
        }
    }

关键理解：
    → slug = 文件夹名称
    → 就是 /code-review 里面的 code-review


【二、用户输入文本解析逻辑（自己手写，不用 cli 解析库）】
================================================================================

用户输入消息，在消息送入 LLM 之前，Harness 做前置预处理。

2.1 规则

    → 判断用户输入字符串是否以 / 开头
    → 分割：第一个空格之前的部分是命令
    → 空格之后是传给 skill 的附加参数

示例：
    输入：/code-review 请重点检查auth模块
    → command = "/code-review"
    → skillSlug = "code-review"
    → restArgs = "请重点检查auth模块"


2.2 伪代码

    /**
     * 用户原始输入预处理
     */
    function parseSlashCommand(userInput: string): {
        isSlashCommand: boolean;
        skillSlug?: string;
        args?: string;
    } {
        const trimed = userInput.trim();

        if (!trimed.startsWith("/")) {
            return { isSlashCommand: false };
        }

        // 去掉开头的 /
        const afterSlash = trimed.slice(1);

        // 在第一个空格切分
        const spaceIndex = afterSlash.indexOf(" ");

        let slug: string;
        let args: string | undefined;

        if (spaceIndex === -1) {
            slug = afterSlash;
            args = undefined;
        } else {
            slug = afterSlash.slice(0, spaceIndex);
            args = afterSlash.slice(spaceIndex + 1).trim();
        }

        return {
            isSlashCommand: true,
            skillSlug: slug,
            args
        };
    }


2.3 查找 skill

    → 拿到 skillSlug
    → 去 SkillRegistry.getBySlug(slug) 查询

    ✅ 找到：这是合法 skill 斜杠命令

    ❗ 找不到：不是 skill
        → 原样把整串文本交给 LLM
        → 允许你随便输入 /something，当成普通文本

⚠️ 注意：
    → 不是所有 /xxx 都一定是 skill
    → 找不到对应 skill，就退化成普通用户消息
    → 不会报错


【三、解析命中之后完整执行流程】
================================================================================

    用户输入：/code-review 检查auth模块
        ↓
    parseSlashCommand 解析出 skillSlug=code-review，args=检查auth模块
        ↓
    SkillRegistry 查询元数据，确认存在
        ↓
    磁盘读取 SKILL.md 完整 markdown 正文
        → 懒加载，此时才读文件
        ↓
    把 skill 完整内容 + 用户附带的 args，组装成 user message
        → push 进当前 agent.messages[]
        ↓
    不再把原始的 `/code-review xxx` 文本发给大模型
        → 替换成 skill 指令文本
        ↓
    进入下一轮 LLM 调用，模型拿到 skill 的 SOP

关键点：
    → 斜杠命令是框架层预处理
    → 不会把 /code-review 这个字符串丢给 Anthropic API
    → 框架拦截替换
    → 真正发给模型的是 skill 的完整 markdown 内容


【四、IDE / CLI 的自动补全】
================================================================================

工作流程：
    → 用户敲 /
    → 前端向后台 harness 请求接口
    → 调用 skillRegistry.listAll()
    → 返回全部 skill 列表
    → UI 下拉展示候选

关键：
    → 全部来源于内存注册表
    → 不需要扫描磁盘
    → 启动阶段已经全部加载元数据


【五、和自动触发（invoke:auto）的区别】
================================================================================

┌────────────────────────┬─────────────────────┬──────────────────────────────┐
│        模式            │  触发来源           │  处理位置                    │
├────────────────────────┼─────────────────────┼──────────────────────────────┤
│  /xxx 手动斜杠命令     │  用户输入文本       │  消息预处理层                │
│                        │                     │  手写字符串解析              │
│                        │                     │  命中后直接 push messages    │
├────────────────────────┼─────────────────────┼──────────────────────────────┤
│  invoke:auto 自动加载  │  模型决策           │  Tool 执行层                 │
│                        │                     │  内置 SkillTool 工具 handler │
│                        │                     │  读取 skill                  │
└────────────────────────┴─────────────────────┴──────────────────────────────┘

两种触发入口，最终效果一样：
    → 把 SKILL.md 完整内容 push 到 messages 数组
    → 只是触发源不同


伪代码对比：

    // 1. 用户斜杠命令路径
    // parseSlashCommand命中 → read SKILL.md → messages.push(userMsg)

    // 2. auto自动触发路径（模型调用SkillTool）
    async function skillToolHandler(input: { skillSlug: string }, ctx: ToolUseContext) {
        const meta = skillRegistry.getBySlug(input.skillSlug);
        const fullContent = fs.readFileSync(meta.filePath + "/SKILL.md", "utf8");
        ctx.messages.push({ role: "user", content: fullContent });
        return { content: "skill loaded" };
    }


【六、为什么不用第三方 CLI 解析库（yargs/commander 等）】
================================================================================

原因：

    → 它不是命令行程序，是聊天消息预处理
    → 输入是用户自由聊天文本，不是严格 cli argv 参数

    → 逻辑非常简单：
        - 只识别 / 前缀
        - 分割第一个空格
        - 不需要子命令、flag、--xxx 参数解析

    → 容错要求高：
        - 匹配不到 skill 就直接当普通聊天文本
        - 不能抛解析错误

    → 需要和聊天消息流深度耦合
        - 第三方命令库是处理进程 argv
        - 不适合聊天文本流

所以：
    → 原版就是手写几十行字符串处理
    → 没有引入解析库


【七、工程复刻注意点（踩坑点）】
================================================================================

⚠️  大小写
    → skill slug 严格等于文件夹名字
    → 区分大小写
    → 输入 /Code-Review 和 /code-review 视为不同

⚠️  懒加载
    → 只有命中斜杠命令那一刻，才去磁盘读完整 SKILL.md
    → 启动阶段只解析 YAML 元数据

⚠️  子 Agent
    → 子 Agent 拥有独立的消息数组
    → 但共享同一个全局 SkillRegistry
    → 子 Agent 内部也可以处理斜杠命令
    → 子 Agent 的 messages 是隔离的
    → 父加载 skill 不会自动给到子

⚠️  找不到 skill
    → 不要报错
    → 原样透传给模型
    → 用户可以随便打斜杠

⚠️  斜杠命令解析发生在消息发给 LLM 之前
    → 属于 Harness 消息预处理阶段
    → 不属于 tool


【八、区分容易混淆的概念】
================================================================================

┌────────────────────────┬─────────────────────┬──────────────────────────────┐
│        模式            │  触发来源           │  处理位置                    │
├────────────────────────┼─────────────────────┼──────────────────────────────┤
│  /skillname 手动斜杠   │  用户输入文本       │  消息预处理层                │
│  命令                  │                     │                              │
├────────────────────────┼─────────────────────┼──────────────────────────────┤
│  invoke:auto 自动加载  │  模型决策           │  Tool 执行层                 │
└────────────────────────┴─────────────────────┴──────────────────────────────┘


【总结一句话】
================================================================================

    会话启动扫描所有 skill，把名字（slug）存入内存 SkillRegistry；
    用户输入消息做简单字符串判断，如果以 / 开头，切分出 skill 名字，去注册表查询；
    命中就读取磁盘 SKILL.md 注入对话，没命中就原样交给模型。
    全程手写字符串逻辑，不用第三方命令解析库。

================================================================================
```



## 多agent

如何让一个 agent 派生子任务而不污染自己的上下文。


### 子agent的好处
1. 为什么需要 Sub Agent：上下文污染问题， sub agent 运行完毕 不需要吧整个 context 给，主agent，减少上下文

### 具体内容
```text
================================================================================
                   Claude Code Subagent（子 Agent）完整原理
================================================================================


【核心定位】
================================================================================

一句话：
    子 Agent 是主 Agent 通过内置 Agent 工具派生出一套独立完整 Agent 执行循环
    （queryLoop），有独立上下文、独立工具权限。

分两种大类：
    1. 普通空白子 Agent
    2. model:inherit Fork 继承子 Agent

关键理解：
    → 不是 Anthropic API 原生能力
    → 全部是 Claude Code 上层应用层实现
    → 底层依然调用 Anthropic Messages API
    → 每个子 Agent 会发起独立 HTTP 请求

形象比喻：
    主 Agent = 项目经理
    子 Agent = 派出去干活的打工人

核心价值：
    → 主 Agent 上下文不会被子 Agent 的大量 read/grep/bash 日志污染
    → 做完任务只把摘要结果回填主会话


【一、两大子 Agent 类型】
================================================================================

1.1 普通 Subagent（空白上下文，默认）
    → 子 Agent 消息历史是空
    → 看不到父的对话历史，只接收父给的任务指令字符串
    → 可以裁剪工具权限：比如只允许 read/grep，禁止 edit/write/bash
    → 可以给子 Agent 专属 system 提示词（比如代码审查专家）

    缺点：
        → 每个子 Agent 完整复制全套上万 token 静态 system
        → 每个子 Agent 都要做一次 cache_creation
        → N 个子任务 ≈ N 倍成本

1.2 Fork Subagent（model:"inherit"，字节级继承前缀，重点）
    → 就是前面聊的缓存继承模式
    → 子 Agent 不继承父内存 / KV 对象
    → 但是复制父的 system 静态块（字节完全一模一样）
    → 复用 Anthropic 服务端的 global prompt-cache

    具体做法：
        分界线 __SYSTEM_PROMPT_DYNAMIC_BOUNDARY 前面静态块完整原样拷贝
        分界线之后，替换成子 Agent 自己专属动态任务信息

    优势：
        → 5 个并发子 Agent 全部命中服务端同一份缓存
        → 成本仅略高于 1 个 Agent

    约束：
        → 必须 5 分钟 TTL 缓存窗口内
        → 静态块不能改动一个字符，否则缓存 miss


【二、子 Agent 完整创建流程】
================================================================================

    1. 主 Agent 调用内置 Agent 工具（旧名叫 task）
        → 工具定义在主 Agent 的 tools 列表
        → 模型输出 tool_use:Agent，携带参数：
            - 任务描述
            - 允许的工具列表
            - model 字段（inherit / 模型名）
            - 是否后台运行 run_in_background

    2. 框架收到 Agent 工具调用，执行 forkSubagent() 函数
        → 分配全新 agentId、全新 AbortController
        → 父取消，子全部跟着取消

    3. 创建隔离的 ToolUseContext
        → 独立会话状态、权限、工作目录副本

    4. 过滤工具集
        → 可以禁用部分工具
        → 防止无限递归（子 Agent 默认禁止再嵌套 fork 子 Agent）

    5. 构建子 Agent 的 system 数组
        → 如果 model:inherit：
            - 直接 deepcopy 父的静态缓存块（字节不动）
            - 替换分界线后的动态部分
        → 如果普通子 Agent：
            - 重新完整生成一套 system

    6. 启动一套全新独立的 queryLoop 主循环
        → 和主 Agent 用同一套代码
        → 五级上下文压缩、hook 钩子、断点 checkpoint
        → 全部自动继承，不用重新写一套

重点：
    → 子 Agent 跑的就是和主 Agent 一模一样的 agent 循环代码
    → 只是上下文隔离


【三、子 Agent 内部执行流程】
================================================================================

    1. 子 Agent 内部循环执行
        → 子 Agent 自己调用 read/edit/bash
        → 拥有自己独立 messages 数组
        → 不会写入父 Agent 的 messages

    2. 子 Agent 执行结束（stop_reason 非 tool_use）
        → 把最终结果压缩成简短报告
        → 以 tool_result 形式返回给主 Agent

    3. 主 Agent 收到的处理
        → 只会收到最终报告
        → 子 Agent 中间所有工具调用日志不会进入主 Agent 上下文
        → 保护主上下文不爆炸

    4. 子 Agent 实例销毁


【四、model 参数四级优先级（子 Agent 选哪个模型）】
================================================================================

    优先级从高到低：

    1. 环境变量 CLAUDE_CODE_SUBAGENT_MODEL（最高）
    2. Agent 工具调用显式传入 model 参数
    3. skill frontmatter 定义的 model 字段
    4. 主对话使用的 model（兜底）
        → 如果这里写 inherit 就开启缓存继承模式


【五、同步 vs 后台异步子 Agent】
================================================================================

同步（默认）：
    → 主 Agent 阻塞等待子 Agent 全部跑完拿到结果
    → 才继续往下走

background 后台异步：
    → 子 Agent 后台跑，主 Agent 继续干活
    → 后续再读取子 Agent 的输出
    → 适合并行多任务


【六、和 Hook、上下文工程的联动】
================================================================================

    → 子 Agent 会继承父的全部 Hook 配置
    → 也可以在 skill frontmatter 单独重写 / 禁用钩子

    → 子 Agent 内部同样完整执行：
        - 静态 / 动态分界
        - 4 个 checkpoint 断点
        - 五级递进上下文压缩

    → PreToolUse、Stop 等钩子在子 Agent 内部也会触发


【七、TS 极简伪代码，复刻核心逻辑】
================================================================================

// 主agent循环
async function mainAgentLoop() {
    while(true) {
        const toolUse = await llmGenerate();

        if(toolUse.name === "Agent") {
            // 派生子Agent
            const subResult = await forkSubAgent({
                task: toolUse.input.task,
                model: toolUse.input.model,        // "inherit" 或者模型名
                allowedTools: toolUse.input.allowedTools
            });

            // 把子Agent结果回填给主agent
            appendToolResult(subResult);
            continue;
        }

        // ...普通工具执行逻辑
    }
}


async function forkSubAgent(options) {
    // 1. 隔离的执行上下文
    const childContext = createSubagentContext();

    let systemPayload;

    if(options.model === "inherit") {
        // inherit模式：字节完全复制父静态块，只替换分界线后的动态部分
        const staticBlock = structuredClone(parentStaticSystemBlock);
        const boundary = {"type":"text","text":"__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"};
        const subDynamic = {"type":"text","text":"SUB_TASK:" + options.task};
        systemPayload = [staticBlock, boundary, subDynamic];
    } else {
        // 普通子agent，完整重新构建system
        systemPayload = buildFreshSystemPrompt();
    }

    // 过滤工具，防止递归fork
    const childTools = filterTools(options.allowedTools);

    // 子agent跑完整独立queryLoop（复用全部压缩、hook逻辑）
    const subOutput = await queryLoop({
        system: systemPayload,
        tools: childTools,
        toolUseContext: childContext,
        messages: [{"role":"user","content":options.task}]
    });

    return subOutput.finalReport;
}


【八、容易踩坑点】
================================================================================

❌ 踩坑 1：model:"inherit" 不是 Anthropic API 参数
    → 传给 API 的 model 字段依然是 claude-3-5-sonnet-20241022
    → inherit 只是上层标记

❌ 踩坑 2：误以为子 Agent 拿到父的 GPU KV 缓存对象
    → 拿不到，只是构造一模一样的文本前缀
    → 命中服务端持久化缓存

❌ 踩坑 3：inherit 模式下静态块改动哪怕一个空格
    → token 序列变化，缓存直接失效

❌ 踩坑 4：子 Agent 会继承父 Hook，容易出现递归死循环
    → 框架有 isInForkChild() 做防护
    → 禁止子 Agent 无限嵌套 fork

❌ 踩坑 5：tools 参数无法被 prompt cache 缓存
    → 每个子 Agent 每次请求都要完整携带 tools schema


【九、对比普通多 Agent 框架（CrewAI/AutoGen）】
================================================================================

CrewAI / AutoGen：
    → 每个 Agent 独立写一套 prompt、独立编排
    → 压缩、钩子、权限都要自己实现

Claude Code 子 Agent：
    → 复用同一套主循环基础设施
        - 压缩
        - hook
        - 断点
        - 错误恢复
    → 只做上下文隔离
    → 维护成本低很多

================================================================================
```


### 如何实现
一： 这个创建agent 作为一个函数

二： 异步agent 如何通信: 主轮询访问
- run_in_background: true 开启异步子 Agent。
- 主 Agent 不会阻塞等待子完成；主、子各自跑独立 queryLoop；两者没有原生 LLM 侧消息通道，靠框架层的共享内存状态 + 特殊工具 + 消息句柄通信，不是模型之间直接对话。
- 注意：底层每个 Agent 依旧各自发独立 Anthropic HTTP 请求，模型之间完全隔离，模型本身不知道对方存在。全部是上层应用做的消息中转。


```text
================================================================================
                   BackgroundTask（后台任务句柄）完整原理
================================================================================


【核心数据结构】
================================================================================

fork 后台子 Agent 时，框架生成一个 taskId（uuid），在服务内存维护一张后台任务
注册表：

    interface BackgroundTask {
        taskId: string;
        status: "running" | "completed" | "error" | "cancelled";

        // 子Agent最终输出报告
        finalReport?: string;
        error?: Error;

        // 子内部日志（可选，可配置是否保存）
        partialLogs?: Array<{type: string, content: string}>;

        // 取消控制器：主Agent可以随时杀掉子
        abortCtrl: AbortController;
    }

关键理解：
    → 注册表存在 Node 进程内存
    → 不是存进 LLM messages
    → 主 Agent 看不到子内部每一步 read/edit/bash


【三个内置通信工具（主 ↔ 后台子 Agent）】
================================================================================

主 Agent 的 tool 列表注入 3 个专门用于后台任务的工具：

工具 1：Agent（fork）
    → 传入 run_in_background: true
    → 返回 taskId
    → 立刻返回工具结果，不等待子结束

工具 2：BackgroundTaskStatus（查询状态）
    → 查询 task 状态：running / completed / error
    → 可以选择拉取部分中间日志

工具 3：BackgroundTaskCancel（取消任务）
    → 通过 taskId 终止后台子 Agent
    → 触发 abort，子 Agent 循环退出

⚠️ 重要限制：
    → 后台子不能主动推消息给主 Agent
    → 子 Agent 跑完，不会自动把结果塞入主的 messages
    → 主 Agent 必须主动轮询 BackgroundTaskStatus 去读取结果


【流程一：启动后台子】
================================================================================

1. 主 Agent 调用 Agent 工具，参数：
    {
        "task": "解析项目依赖",
        "model": "inherit",
        "run_in_background": true,
        "allowedTools": ["read", "grep", "glob"]
    }

2. 框架 fork 子 Agent
    → 丢进后台异步执行，不 await 子循环
    → 把 task 注册进后台 task map

3. Agent 工具立刻返回给主 Agent：
    {"taskId": "xxx-uuid"}

4. 主 Agent 继续自己的 queryLoop
    → 继续处理别的任务，不会卡住

5. 子 Agent 在后台独立跑自己的循环
    → 有自己独立 messages、hook、上下文压缩


【流程二：主 Agent 获取子结果】
================================================================================

1. 主 Agent 过几轮之后，调用工具 BackgroundTaskStatus(taskId)

2. 判断状态：
    → 如果状态 = running：
        - 返回还在运行，附带简短进度摘要
        - 主 Agent 继续做别的事，后续再查

    → 如果状态 = completed：
        - 返回 finalReport
        - 主 Agent 拿到报告，作为 tool_result 写入自己的上下文

    → 如果状态 = error：
        - 返回错误信息


【子 Agent 如何向主传递信息？】
================================================================================

    → 子 Agent 没有专门 send_to_parent 工具

    → 子 Agent 所有输出只能保存在框架内存里的 BackgroundTask.finalReport

    → 子 Agent 结束（stop）的时候，框架把子 Agent 的最终总结文本，写入这个字段

    → 子中间过程的 tool 调用日志，默认不会暴露给主 Agent
        - 可以配置开启 partialLogs


【能不能双向通信？】
================================================================================

1. 主 → 子：传递新指令
    → 原生没有 "给正在运行的后台子追加消息"
    → Claude Code 后台子 Agent 一旦启动，它的 messages 数组是封闭的
    → 外部不能直接追加用户消息

    两种方案：
        a. 不支持热更新指令：
            - 如果需要给子新任务
            - 一般做法：cancel 旧 task，重新 fork 一个新的后台子 Agent

        b. 高级扩展（框架层）：
            - 给子 Agent 暴露一个消息队列
            - 主把新指令丢入队列
            - 子 Agent 每一轮循环开始，框架把队列消息注入子的 messages
            - ⚠️ Claude Code 原版没有做这个，属于可扩展点

2. 子 → 主：推送消息
    → 原版子不能主动推送
    → 子 Agent 模型没有 API 可以主动回调主 Agent
    → 只能主轮询

对比：
    → 很多多 Agent 框架比如 AutoGen 支持双向消息队列
    → Claude Code 后台子设计是单向拉取模式，简化复杂度


【model:inherit 在异步后台子的表现】
================================================================================

    → 后台子同样可以使用 model:"inherit"
    → 复用父的 global prompt cache

    约束不变：
        → 必须 5 分钟 TTL 内
        → 静态块字节完全一致

    优势：
        → 多个后台子并发
        → 全部命中同一份服务端 KV 缓存
        → 并发成本很低


【同步子 vs 异步后台子对比】
================================================================================

┌──────────────────────┬───────────────────┬────────────────────────────────────┐
│      模式            │ run_in_background │  行为                              │
├──────────────────────┼───────────────────┼────────────────────────────────────┤
│ 普通同步子 Agent     │ false             │ 主阻塞等待子完成                   │
│                      │                   │ fork 之后直接返回最终报告           │
│                      │                   │ 直接回填 tool_result               │
├──────────────────────┼───────────────────┼────────────────────────────────────┤
│ 异步后台子 Agent     │ true              │ 主不阻塞，子后台跑                 │
│                      │                   │ 返回 taskId                        │
│                      │                   │ 主靠 BackgroundTaskStatus 轮询     │
│                      │                   │ 拉取结果                           │
└──────────────────────┴───────────────────┴────────────────────────────────────┘


【伪代码模拟异步实现】
================================================================================

// 全局内存注册表
const backgroundTasks = new Map<string, BackgroundTask>();


async function forkSubAgent(options: ForkOpts) {
    const taskId = crypto.randomUUID();
    const abortCtrl = new AbortController();

    // 启动，但是不 await！丢进后台
    (async () => {
        try {
            const result = await queryLoop({
                system: buildSystem(options),
                tools: filterTools(options.allowedTools),
                signal: abortCtrl.signal
            });

            // 子跑完，把结果写到内存注册表
            backgroundTasks.get(taskId).status = "completed";
            backgroundTasks.get(taskId).finalReport = result.finalReport;

        } catch(e) {
            const task = backgroundTasks.get(taskId);
            task.status = "error";
            task.error = e;
        }
    })();

    // 立刻注册，返回句柄，不等子执行完毕
    backgroundTasks.set(taskId, {
        taskId,
        status: "running",
        abortCtrl
    });

    return { taskId };
}


// 查询状态工具处理器
async function handleBackgroundTaskStatus(input: { taskId: string }) {
    const task = backgroundTasks.get(input.taskId);

    if(!task) return { error: "task not found" };

    return {
        taskId: task.taskId,
        status: task.status,
        finalReport: task.finalReport,
        error: task.error?.message
    };
}


【工程上的坑】
================================================================================

⚠️  内存存储
    → 后台任务存在进程内存
    → 进程重启全部丢失，没有持久化

⚠️  轮询开销
    → 主 Agent 要反复调用 BackgroundTaskStatus
    → 模型可能忘记轮询
    → 导致后台子跑完，但主永远不去读取结果

⚠️  取消
    → 必须传递 AbortController
    → 否则后台子就算主不管，还会继续消耗 token 跑

⚠️  model:inherit 缓存 TTL
    → 如果后台子运行超过 5 分钟
    → 后续子自己的 API 请求会 cache miss

⚠️  后台子内部的五级压缩、hook 依旧正常执行
    → 会消耗 token


【和其他组件的关系】
================================================================================

Hook：
    → 后台子会继承父全部 hook
    → PreToolUse/PostToolUse 在子内部正常触发

上下文压缩：
    → 子 Agent 拥有自己独立的 messages
    → 独立执行五级压缩
    → 不会污染主 Agent 上下文

model:inherit：
    → 后台子同样可以复用父 prompt cache
    → 适合大量并发后台任务


【关键设计思想】
================================================================================

    Claude Code 后台异步 Agent，不是两个模型互相发消息对话。

    它是：
        → 父派出去一个独立作业，拿到任务 ID
        → 父有空就去内存里查作业进度

    核心特点：
        → 子不能主动上报，只能被查询

================================================================================
```






## 总结思想维度
1.工程约定标记 >>> prompt 
重点：SYSTEM_PROMPT_DYNAMIC_BOUNDARY（静态和动态的提示词的分隔符） 不是给模型看的业务指令，是工程架构约定标记。不靠文档告诉开发 “哪部分能改哪部分不能改”，直接用字符串作为分割边界，代码可以自动解析切分，强制执行架构约束。








































# 项目完善
## 交互层面如何方便和多个cli对接
### 交互界面
1. web
2. cli
3. 桌面

目前先处理cli：typer框架处理启动参数。 typer：sys.argv

### /命令的处理
一： Textual 的ui 获取到对应的 string 自己处理： 内置系统命令 / skill / mcp调用 / task
- 系统命令直接执行
- skill命令： 读取 SKILL.md，把 skill 内容追加到 messages 数组。
- mcp命令： /mcp postgres query select * from user   ——》解析后，框架帮你构造对应的 mcp__postgres__xxx tool_use，送入工具调度。
- task： 给大模型
- 自定义命令： 


二： 解析 / 命令的大致逻辑
```text
Textual Input输入框
    ↓ 用户回车
拿到原始字符串
    ↓ parse_slash_command() 自己手写解析
├─系统命令(/new /clean) → 框架直接执行，返回UI提示，不进LLM
├─Skill命令 → 读SKILL.md → push messages，返回UI提示
├─/mcp扩展命令 → 构造mcp__xxx tool_use送入工具调度
├─找不到命令 → 降级普通消息
└─不是斜杠命令 → 普通聊天送入Agent Harness
```

三： 命令提醒和自动补全
```text
================================================================================
                    Textual 斜杠命令补全实现方案
================================================================================


【核心思路】
================================================================================

Textual 没有现成的斜杠补全组件，需要自己实现。


【实现步骤】
================================================================================

1. 监听输入变化
    → 绑定 Input 组件的 on_change 事件
    → 实时检测用户输入

2. 判断是否触发补全
    → 检测输入框文本是否以 / 开头
    → 提取当前输入的命令前缀

3. 生成候选列表
    → 拼接来源：系统内置命令 + Skill 名称 + mcp名称
    → 根据当前输入前缀过滤匹配

4. 弹出下拉面板
    → 使用 Overlay 悬浮层
    → 用 Listview 展示候选列表
    → 位置：紧贴输入框下方

5. 键盘交互
    ↑↓ 键 → 上下移动选中项
    Tab / Enter → 选中回填到输入框
    Esc → 关闭下拉面板


【关键设计点】
================================================================================

下拉框仅 UI 辅助
    → 补全是展示层辅助，不改变底层逻辑
    → 真正解析仍依赖 parse_slash_command 函数

Tab 键拦截
    → Tab 默认行为是切换焦点
    → 补全场景下需拦截重写
    → 改为：选中当前高亮项 + 回填输入框


【与后端解析的协作】
================================================================================

补全层（UI）：展示 /xxx 候选
解析层（框架）：parse_slash_command 做实际解析

两者完全解耦：
    补全只负责让用户方便输入
    解析负责判断命中哪个 skill / 命令


【常用 Skill 命令】
================================================================================

/skillname → 触发 Skill
/memory    → 记忆管理
/clear     → 清空上下文
/cost      → 查看 Token 用量
/model     → 切换模型
/help      → 帮助
/exit      → 退出

================================================================================
```



#### 自定义命令如何处理
```text
================================================================================
                    Claude Code 自定义命令（Custom Commands）
================================================================================


【核心概念】
================================================================================

自定义命令 = 用户可定义的斜杠命令，用于封装常用工作流/提示词模板。

与 Skill 的区别：
    Skill：封装完整 SOP，可 auto 触发，有 YAML frontmatter，含元数据
    自定义命令：轻量快捷方式，仅手工斜杠触发，就是一段文本模板


【创建方式】
================================================================================

目录：项目根目录下 .claude/commands/

规则：文件名 = 命令名
    .claude/commands/review-pr.md  → 对应 /review-pr
    .claude/commands/deploy.md     → 对应 /deploy


【内容编写】
================================================================================

就是纯 Markdown，写清楚任务指令即可。

示例 .claude/commands/review-pr.md：
    分析当前PR的变更，检查代码风格、潜在bug，
    并生成一份简洁的审查报告，重点关注性能和安全问题。


【典型应用场景】
================================================================================

/review-pr    → 统一团队代码审查流程
/deploy-test  → 一键部署到测试环境
/test-gen     → 按团队规范生成单元测试


【命令与 Skill 的区别】
================================================================================

┌──────────────┬─────────────────────┬─────────────────┬─────────────────┐
│              │  自定义命令          │  Skill          │                  │
├──────────────┼─────────────────────┼─────────────────┼─────────────────┤
│  触发方式    │  仅手工 /xxx        │  手工 /xxx      │                  │
│              │                     │  或 auto 自动   │                  │
├──────────────┼─────────────────────┼─────────────────┼─────────────────┤
│  元数据      │  无 YAML            │  有 YAML        │                  │
│              │                     │  frontmatter    │                  │
├──────────────┼─────────────────────┼─────────────────┼─────────────────┤
│  复杂度      │  轻量文本模板        │  完整 SOP 手册  │                  │
├──────────────┼─────────────────────┼─────────────────┼─────────────────┤
│  存储位置    │  .claude/commands/  │  .claude/skills/│                  │
└──────────────┴─────────────────────┴─────────────────┴─────────────────┘


【本质】
================================================================================

自定义命令 = 用户自定义的快捷提示词

- 没有 YAML frontmatter
- 只有 markdown 正文
- 输入 /xxx 时，框架读取对应 .md 文件内容，注入 messages
- 比 Skill 更轻量，适合团队内共享高频操作模板

================================================================================
```


## model
1. 本地化配置，配置apikey，baseurl
2. 每次发起请求进行文件读取，调用llm进行发送，（类似于热更新）



## agent
1. 去掉意图识别，直接使用tools判断
2. 工具定义修改 添加是不是可以并行调用，穿行的直接放到queue中 

### 模式
1. auto： 全自动，全部工具放行  避免执行rm -f命令
2. plan： 先输出计划,放到请求id_todo.md文件里面，人工确认后再执行
3. approve： 每一个高危工具（edit/write/bash/MCP 写）都要人工审批确认
4. edit： 只允许文件读写类工具，禁用 bash、网络 MCP 等




## 提示词


## 记忆
1. 类别： 项目级别的，用户级别的
2. 记录那些文件，每个文件记录那些东西
3. 类型： 长期记忆（用户偏好等），短期记忆（对话历史），跨会话记忆（一个项目多个会话，如果保证多个会话的记忆相通）
4. 如何压缩： 参考 claude code：
- tool调用：read,grep,grop读取的大数据文件内容 直接记录到session的临时文件，记忆里面直接引用文件路径， 清理过期的tool调用（）
- token 到达安全阈值触发；fork 一个禁止调用工具的独立子 Agent，用 9 段结构化模板把整个会话生成结构化摘要； 内存里旧 messages 替换成这一条 summary 消息； 用户手动命令 /compact 也会触发这一级。
- api返回413： 删除最久远的消息，保证请求能发出去，最后兜底手段。



5. 做梦功能


### 问题
一： 如何判断过期的工具调用
1. 对文件操作的工具结果会失效；纯只读不碰文件的搜索、glob 一般不会过期， 
```text
# key: 文件绝对路径；value:最后一次修改操作的消息id
file_state_map: dict[str, str] = {}

会更新 file_state_map 的工具（写操作）
    edit / write / bash（会修改磁盘文件）/ MCP 写文件
    当执行成功，就记录：file_state_map[abs_path] = 当前tool_result的message_id

判定一条 read 工具结果是否过期逻辑
这条 tool 是read，读取文件 A；
文件 A 存在于file_state_map；
read 对应的 message_id < 文件 A 最后被修改的 message_id
→ 判定：这条 read 结果过期。
消息 id 是按对话顺序递增，越后面消息 id 数字越大。
逻辑：读完文件之后，发生过修改，旧的 read 内容已经不准。
```

2. read / bash / grep / glob / web_search / web_fetch （/ edit / write 可选）可以进行过期


二： 两个快照的区别
1. file_state_map：agent 内部操作，用于上下文压缩清理过期 read 结果
2. SnapshotTracker：对比磁盘真实状态，防止覆盖用户外部修改。


## 上下文管理

## tool
工具的裂隙： readOnly ： true 可以 read glob grep webfetch 并想起来， mutating 工具（Edit / Write / Bash）则严格串行，避免文件竞态


### rag_tool
```json
tool_def = {
    "name": "local_knowledge",
    "description": "检索本地私有知识库，查询项目文档、业务说明、历史笔记。适合查询不能直接读取的大量文档集合。",
    "is_concurrency_safe": True,  # 只读，可以并行执行
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type":"string","description":"用户检索查询语句"},
            "top_k": {"type":"integer","default":3,"description":"返回片段数量"}
        },
        "required": ["query"]
    }
}
```


## mcp
1. 初始化链接，调用获取所有工具
2. system prompt的 动态注入
3. 吧mcp的tool 注入到系统中，且添加hook


## skill
1. system prompt的 动态注入



## hook如何搞
1. 扩展loop循环
2. 用户配置的hook，如何在程序中识别： 正则表达式 还是 解析json——》固定key——》解析value数组，封装对象


## 回滚 / 检查点
检查点回滚方案： 依靠备份 / git操作 

## 可观测性
1. 链路追踪： 事件上报接口编写，对接各个三方
2. 





## 安全
### 沙箱问题
1. 执行命令必须是在当前沙箱 / 工作空间中

### tool的安全问题


### 人工审批 gate如何





## 彩蛋功能
### 像素宠物
孵化一个 ASCII 像素宠物
系统基于用户 ID 的 hash 确定性分配 18 种物种（CSDN 列出全名：duck / goose / blob / cat / dragon / octopus / owl / penguin / turtle / snail / ghost / axolotl / capybara / cactus / robot / rabbit / mushroom / chonk），每种拥有 5 个稀有度分级（Common 到 Legendary）、属性系统（SNARK、WISDOM 等 stats）和装饰系统（帽子等）。宠物会根据任务执行进度表现出不同的情绪状态动画。



### 做梦功能




























