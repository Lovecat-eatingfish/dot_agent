<p align="center">
  <img src="./logo.png" alt="MokioClaw Logo" width="460" />
</p>

<h1 align="center">MokioClaw</h1>

<p align="center">
  从零开始，一步步丰富一个真正能做事的 Agent 系统。
</p>

## 项目主旨

MokioClaw 是一个教学优先的 Mini CodeAgent 项目。它按 Agent 系统自然生长的路径推进：先从 ToolCall 触碰文件和命令行开始，再升级到 LangGraph 显式工作流，然后继续引入 MultiAgent、Context Engineering、Harness Engineering、Skill 和更完整的 Claw 产品壳。

项目的核心不是“写一个神秘黑盒”，而是把 Agent 如何规划、调用工具、交接子 Agent、验证结果讲清楚。每个阶段都要能运行、能展示、能解释。

## 当前阶段

当前处于第 6 阶段的第一步：在 MultiAgent、Context Engineering、Harness Engineering 基础上，新增 Textual TUI 本地交互层。飞书 API 交互仍属于下一步。

第 3 阶段的图不再是固定的 `planner -> searchAgent -> codeAgent -> verifier` 顺序链路。现在外层 LangGraph 只有 supervisor 与验收循环：

```text
User Task
   |
   v
planner / supervisor
   |  toolcall: CallSearchAgentTool
   |-----------------------------> searchAgent
   |<----------------------------- research notes + sources
   |
   |  toolcall: CallCodeAgentTool
   |-----------------------------> codeAgent
   |<----------------------------- files + command results
   |
   v
context_monitor
   | token >= limit
   v
context_compressor
   |
   v
verifier / model reviewer
   | pass
   v
final
   ^
   | fail and attempts < max_attempts
   +--------- planner
```

标志性演示任务：

```bash
uv run mokioclaw "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```

这个任务适合展示 MultiAgent，因为它需要先查资料，再把资料转成可交付的 HTML 页面，最后由模型版 verifier 读取文件、执行检查、判断是否完成。第 4 阶段会在 planner/verifier 之间自动监控上下文 token，达到阈值后插入压缩节点，并把规则、工作记忆、历史摘要拆成独立层次展示。

## Context Engineering

### 自动压缩

自动压缩不是简单追加一段摘要。`context_compressor` 会使用 LangGraph 的 `RemoveMessage(REMOVE_ALL_MESSAGES)` 真正清空旧 `messages`，再写入一条压缩后的上下文摘要 message。这样后续节点看到的是更小的窗口，而不是越来越长的 transcript。

压缩会保留：

- 用户任务、当前计划、todo、验收标准、验证命令
- searchAgent 的研究结论和来源链接
- codeAgent 的产物、重要文件和执行摘要
- verifier 的失败原因、下一步建议和风险
- workspace 内 `TODO.md` 和 `NOTEPAD.md` 中的持久上下文

默认压缩阈值是 `400000` token，可通过环境变量调整。为了演示压缩效果，可以临时设置小阈值：

```bash
MOKIO_CONTEXT_TOKEN_LIMIT=2000 uv run mokioclaw "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```

### 分层记忆

分层记忆把原来散落在 `messages`、state、`TODO.md` 和 `NOTEPAD.md` 中的信息收束成一个 `Memory Snapshot`。节点 prompt 不再各自手写拼接大段上下文，而是统一读取三层记忆：

| Memory 层 | 来源 | 用途 |
| --- | --- | --- |
| `rules` | 系统自动生成 | 稳定规则、workspace 边界、文件职责，不暴露给 Agent 改写 |
| `working_memory` | graph state / `TODO.md` | 当前任务、计划、todo、验收标准、验证命令、来源、handoff、最近错误 |
| `history_summary_store` | `NOTEPAD.md` / `HISTORY_SUMMARY.md` / `context_summary` | 长期笔记、压缩后的历史摘要、最近压缩事件 |

运行时终端会展示 `Memory Snapshot` 面板，显示三层摘要、todo 数、source 数、handoff 数，以及 `NOTEPAD.md` 和 `HISTORY_SUMMARY.md` 是否存在。这样可以直观看到 Context Engineering 不只是删消息，还把不同类型的信息放到了不同层里。

## 当前 Tool 与 Agent 架构

| 节点 / Agent | 使用工具 | 职责 |
| --- | --- | --- |
| `planner` | `TodoWriteTool` / `CallSearchAgentTool` / `CallCodeAgentTool` | 制定计划，并通过 toolcall 分派专家 Agent |
| `searchAgent` | `WebSearchTool` | 调用 Tavily 搜索资料，返回研究摘要和 sources |
| `codeAgent` | `FileReadTool` / `FileWriteTool` / `FileEditTool` / `GrepTool` / `BashTool` / `TodoUpdateTool` | 写文件、运行检查、更新 todo 进度 |
| `context_monitor` | 无 | 估算当前上下文 token，决定是否触发压缩 |
| `context_compressor` | 模型压缩 prompt / 分层记忆 | 删除旧 messages，生成可恢复的压缩上下文，并写入 `HISTORY_SUMMARY.md` |
| `verifier` | `FileReadTool` / `GrepTool` / `BashTool` / `WebSearchTool` | 模型验收节点，只读检查，不修改文件 |
| `final` | 无 | 汇总计划、来源、验收结果和运行方式 |

当前工具：

| Tool | 职责 | 设计重点 |
| --- | --- | --- |
| `TodoWriteTool` | 写入 todo、验收标准、验证命令 | 让 planner 的计划外显；兼容列表、JSON 列表和以 todo id 为 key 的 description 字典 |
| `TodoUpdateTool` | 更新 todo 状态 | 同步更新 state 和 workspace 内的 `TODO.md` |
| `CallSearchAgentTool` | 调用 searchAgent | 把子 Agent 包装成 planner 的 toolcall |
| `CallCodeAgentTool` | 调用 codeAgent | 把实现专家包装成 planner 的 toolcall |
| `NotepadReadTool` | 读取长期笔记 | 从 workspace 的 `NOTEPAD.md` 恢复上下文 |
| `NotepadAppendTool` | 追加长期笔记 | 记录发现、决策、重要文件、风险和下一步 |
| `WebSearchTool` | Tavily 网络搜索 | 返回 answer 和结构化 sources |
| `FileReadTool` | 读取 workspace 内文本文件 | 支持 `offset` / `limit`，记录“已读状态” |
| `FileWriteTool` | 创建文件或整文件写入 | 覆盖已有文件前要求先读 |
| `FileEditTool` | 对已有文件做局部替换 | 基于 `old_text` / `new_text`，要求唯一匹配 |
| `GrepTool` | 搜索 workspace 内文本内容 | 用结构化方式定位内容 |
| `BashTool` | 执行开发命令 | 固定 workspace、fresh shell、可配置超时、长输出落盘、后台任务、env file 注入、基础安全拦截；高风险命令需要人类审批 |

## Harness Engineering

当前先引入 Harness Engineering 的三块能力：human-in-the-loop approval、checkpoint/resume、trace 链路观测。`BashTool` 被包成更像真实 harness 的执行层，CLI 运行时则持续保存可恢复的工作快照和可读的运行日志。这里参考的是 Claude Code 公开文档里可见的机制：命令权限/审批、默认和最大 timeout、fresh shell 执行、环境注入、长输出处理、后台任务、可恢复工作状态、运行观测。Claude Code 源码和内部提示未公开，因此这里不照搬源码，而是实现同类边界能力。

默认审批模式是 `inline`：

```bash
uv run mokioclaw --approval-mode inline "搭建一个 FastAPI Todo 后端，并运行检查"
```

当 Agent 尝试运行 `uv add fastapi`、`pip install fastapi`、`npm install`、`curl ...`、`uvicorn ...` 等命令时，CLI 会展示命令和风险原因，并询问 `Approve? [y/N]`。输入 `y` 或 `yes` 才会执行，其余输入会拒绝该命令并把结构化失败结果返回给 Agent。

`BashTool` 每次调用都会启动 fresh shell，`export FOO=bar` 这类临时环境变量不会跨工具调用保留。执行环境会生成 `.mokioclaw/shims` 并放到 `PATH` 前面，把 `python`、`python3`、`pip`、`pip3` 稳定指向当前运行 MokioClaw 的 Python；同时会优先加入 workspace 的 `.venv/bin`、`venv/bin` 和 `node_modules/.bin`，减少工具链漂移。需要跨命令复用的环境变量可以写入 workspace 下的 `.mokioclaw.env`，或用 `MOKIO_BASH_ENV_FILE` 指向一个 env 文件；env 文件支持 `export KEY=value` 和 `PATH=.venv/bin:$PATH` 这类变量展开。普通命令默认最多等待 120 秒，最大允许 600 秒；长输出会截断展示，并把完整 stdout/stderr 写到 workspace 的 `.mokioclaw/bash-outputs/`。长时服务应通过 `run_in_background=true` 启动，输出会落到 `.mokioclaw/background/`。

可用模式：

| 模式 | 行为 |
| --- | --- |
| `inline` | 高风险命令在 CLI 中询问人类审批 |
| `deny` | 高风险命令一律拒绝，适合测试和非交互运行 |
| `auto` | 高风险命令自动批准，适合受控演示 |

Checkpoint 默认开启轻量模式：

```bash
uv run mokioclaw --checkpoint-mode light "搭建一个 FastAPI Todo 后端，并运行检查"
```

如果中途按 `Ctrl+C`，MokioClaw 会在 workspace 内写入 `.mokioclaw/checkpoints/RECOVERY.md` 和 `checkpoint.json`，并在 CLI 里打印恢复命令：

```bash
uv run mokioclaw --resume .mokioclaw/workspaces/workspace-YYYYMMDD-HHMMSS-xxxxxx
```

可用 checkpoint 模式：

| 模式 | 行为 |
| --- | --- |
| `light` | 默认模式；保存 workspace 文件快照、`TODO.md`、`NOTEPAD.md`、`HISTORY_SUMMARY.md`、恢复摘要和内部 git 文件版本，恢复时让模型基于这些上下文继续任务 |
| `strict` | 在 light 基础上保存可序列化 graph state 和事件日志；恢复时执行 state-backed restart，若 state 不可读则自动降级 light resume |
| `off` | 不保存 checkpoint |

checkpoint 文件都位于当前 workspace 的 `.mokioclaw/checkpoints/`，内部 git repo 只用于该 workspace 文件快照，不会接管项目仓库。运行中会在开始、graph update、失败/审批类工具结果、中断和结束这些关键安全点刷新 checkpoint；完整事件链路由 Trace 记录。

Trace 默认开启，并在每次运行结束或中断时展示 `Trace Summary`：

```bash
uv run mokioclaw --trace-mode on "搭建一个 FastAPI Todo 后端，并运行检查"
```

Trace 文件位于当前 workspace 的 `.mokioclaw/traces/trace-*/`：

| 文件 | 作用 |
| --- | --- |
| `events.jsonl` | 顺序记录 run、graph update、tool call/result、handoff、checkpoint 等结构化摘要事件 |
| `summary.json` | 记录节点访问次数、工具调用数、失败工具数、审批数、checkpoint 数和最终状态 |
| `timeline.md` | 给人类阅读的简短运行时间线 |

Trace 只做观测，不改变 Agent 行为，也不替代 checkpoint/resume。

## 交互层：Textual TUI

除了原来的 Rich 时间线输出，MokioClaw 现在提供一个 Textual 终端界面：

```bash
uv run mokioclaw tui
```

也可以启动后立即执行一个任务：

```bash
uv run mokioclaw tui "帮我创建一个简易的贪吃蛇游戏代码，并执行检查"
```

TUI 使用 `stream_session_events()` 维护一个持续 coding session：一次 TUI 会话默认绑定同一个 workspace，后续输入会带着 session history、TODO、分层 memory、checkpoint 和 trace 继续推进；一次性 Rich CLI 仍使用单轮 `stream_agent_events()`。

- 顶部展示 MokioClaw 状态和无文字像素 logo。
- 中间展示事件时间线：plan、tool call/result、handoff、verifier、final、checkpoint、trace summary。
- 右侧展示当前 session、workspace、todo、工具调用数、审批数、checkpoint 和 trace 路径。
- 底部输入消息，按 Enter 发送；一个 turn 完成后可以继续输入下一轮。
- 默认多轮复用同一个 workspace；输入 `/new` 可以切换到一个全新的 session workspace。
- 高风险 BashTool 命令会在 TUI 内弹出审批对话框，支持 `y` / Enter 批准，`n` / Esc 拒绝。

为了避免“你好”这类输入也启动完整复杂流程，LangGraph 前面增加了一个模型路由节点 `intent_router`。它会先结合当前输入和 session context 判断应该走轻量 `chat_responder`，还是进入 planner / codeAgent / verifier 复杂工作流。寒暄、感谢、帮助说明和普通概念问答会直接通过 `chat_response` 回复，只写入 session 记录，不进入 planner，也不会写 checkpoint/trace。需要创建/修改文件、运行命令、搜索资料、验证结果、检查项目，或者“继续/修一下/运行测试”这类引用当前 workspace 的后续指令时，才进入完整 MultiAgent 工作流。

TUI 子命令支持和普通 CLI 相同的运行选项：

```bash
uv run mokioclaw tui --approval-mode inline --checkpoint-mode light --trace-mode on
uv run mokioclaw tui --workspace .mokioclaw/workspaces/my-session
uv run mokioclaw tui --resume .mokioclaw/workspaces/workspace-YYYYMMDD-HHMMSS-xxxxxx
```

原有一次性 CLI 仍然保留：

```bash
uv run mokioclaw "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```

## 文件目录

```text
MokioAgent/
├─ assets/
│  └─ logo-no-words.png
├─ logo.png
├─ README.md
├─ pyproject.toml
├─ uv.lock
├─ src/
│  └─ mokioclaw/
│     ├─ agents/
│     │  ├─ search_agent.py     # searchAgent：Tavily 研究专家
│     │  └─ code_agent.py       # codeAgent：文件和命令执行专家
│     ├─ cli/
│     │  ├─ app.py              # Typer CLI 入口与 tui 子命令
│     │  ├─ formatter.py        # Rich 事件时间线展示
│     │  ├─ event_summary.py    # Rich CLI / Textual TUI 共用事件摘要
│     │  └─ tui/                # Textual 本地交互层
│     ├─ core/
│     │  ├─ agent.py            # LangGraph workflow 运行入口
│     │  ├─ checkpoint.py       # light / strict checkpoint 与 resume
│     │  ├─ paths.py            # 项目根目录与 workspace 路径
│     │  └─ state.py            # RuntimeState 与文件快照
│     ├─ graph/
│     │  ├─ state.py            # Graph state
│     │  ├─ memory.py           # rules / working memory / history summary-store
│     │  ├─ nodes.py            # planner / context monitor / compressor / verifier / final
│     │  └─ workflow.py         # StateGraph 组装与路由
│     ├─ providers/
│     │  └─ openai_provider.py  # 从 .env 创建 ChatOpenAI
│     ├─ prompts/
│     │  ├─ stage3.py           # 第 3 阶段节点与子 Agent prompt
│     │  └─ stage4.py           # Context compression prompt
│     └─ tools/
│        ├─ registry.py         # 工具注册
│        ├─ todo_tool.py        # TodoWrite / TodoUpdate / TODO.md 持久化
│        ├─ notepad_tool.py     # NOTEPAD.md 长期工作笔记
│        ├─ web_search_tool.py  # Tavily WebSearchTool
│        ├─ file_tools.py       # Read / Write / Edit
│        ├─ grep_tool.py        # 内容搜索
│        └─ bash_tool.py        # 命令执行
└─ tests/
   ├─ test_tools.py
   ├─ test_graph.py
   ├─ test_formatter.py
   └─ test_cli_smoke.py
```

运行时会自动创建：

```text
.mokioclaw/
└─ workspaces/
   └─ workspace-YYYYMMDD-HHMMSS-xxxxxx/
      ├─ TODO.md       # 当前任务计划、todo、验收标准和验证命令
      ├─ NOTEPAD.md    # 长期工作笔记，压缩后仍可恢复关键信息
      ├─ HISTORY_SUMMARY.md # 压缩后的历史摘要 store
      ├─ SESSION_SUMMARY.md # TUI 多轮 session 的可读摘要
      ├─ .mokioclaw/
      │  ├─ session/
      │  │  └─ session.json    # TUI session 的结构化 turn/history 状态
      │  ├─ bash-outputs/       # BashTool 长输出落盘
      │  ├─ background/         # 后台任务输出
      │  ├─ traces/
      │  │  └─ trace-*/         # 结构化链路观测日志
      │  └─ checkpoints/
      │     ├─ checkpoint.json  # checkpoint 元数据和工作摘要
      │     ├─ RECOVERY.md      # light resume 使用的恢复摘要
      │     ├─ state.json       # strict 模式的可序列化 graph state
      │     ├─ events.jsonl     # strict 模式事件日志
      │     └─ git/             # 内部 workspace 文件快照仓库
      └─ ... Agent 生成的代码、页面、测试和运行产物
```

默认每次新任务都会创建一个新的 `workspace-*` 目录，避免不同任务互相污染。需要复用或指定目录时，可以显式传入 `--workspace`。

## 示例执行链路

用户输入：

```bash
uv run mokioclaw "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```

指定 workspace：

```bash
uv run mokioclaw --workspace .mokioclaw/workspaces/demo "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```

典型链路：

1. CLI 创建 workspace 和 `RuntimeState`。
2. `planner` 生成计划：搜索资料、创建 `amiya_profile.html`、加入来源链接、运行检查。
3. `planner` 通过 `CallSearchAgentTool` 把研究任务交给 `searchAgent`。
4. `searchAgent` 调用 `WebSearchTool`，从 Tavily 返回摘要和来源链接。
5. `planner` 收到 research notes 和 sources 后，通过 `CallCodeAgentTool` 把实现任务交给 `codeAgent`。
6. `codeAgent` 调用 `TodoUpdateTool` 标记进度，调用 `FileWriteTool` 写出 HTML，并用 `BashTool` 做非交互检查。
7. `codeAgent` 可以调用 `NotepadAppendTool` 把重要发现、文件、风险和下一步写入 `NOTEPAD.md`。
8. 各节点生成 `Memory Snapshot`，把 rules、working memory、history summary-store 分层注入 prompt 并在终端展示。
9. `context_monitor` 估算 token；达到阈值时进入 `context_compressor` 压缩消息窗口。
10. 压缩摘要会进入 state 的 `history_summary`，并持久化到 workspace 的 `HISTORY_SUMMARY.md`。
11. `verifier` 作为模型验收节点读取分层记忆，再调用 `FileReadTool` / `GrepTool` / `BashTool` / `WebSearchTool` 检查结果。
12. 如果 verifier 失败，失败原因和下一步建议回到 `planner`，planner 再次分派子 Agent 修复。
13. 运行期间 checkpoint 会持续刷新；中断后可用 `--resume <workspace>` 让模型基于恢复摘要和 workspace 文件继续任务。
14. Trace 旁路记录节点、工具、handoff、checkpoint 和最终状态，结束时 CLI 展示 `Trace Summary`。
15. 如果 verifier 通过，进入 `final`，输出文件、来源、验收结论、压缩次数和运行方式；CLI 会直接展示这个最终总结。

这一阶段的教学价值是：MultiAgent 不只是“多个节点排队”，还可以是 supervisor 把 specialist agent 当成工具调用，让交接、上下文和职责边界都在终端里清楚呈现。

更完整的 workspace 生命周期、checkpoint、shims、bash-outputs 讲解见 [docs/workspace-lifecycle.md](docs/workspace-lifecycle.md)。

## 运行方式

`.env` 配置：

```text
API_KEY=...
MODEL=...
BASE_URL=...
TAVILY_API_KEY=...
MOKIO_CONTEXT_TOKEN_LIMIT=400000
MOKIO_BASH_DEFAULT_TIMEOUT_SECONDS=120
MOKIO_BASH_MAX_TIMEOUT_SECONDS=600
MOKIO_BASH_MAX_OUTPUT_CHARS=6000
MOKIO_BASH_ENV_FILE=
MOKIO_CHECKPOINT_MODE=light
MOKIO_TRACE_MODE=on
```

同步依赖：

```bash
uv sync
```

运行测试：

```bash
uv run pytest -q
```

运行 Agent：

```bash
uv run mokioclaw "帮我查阅明日方舟阿米娅，并编写一个 HTML 介绍人物"
```
