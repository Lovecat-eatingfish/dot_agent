# dot_agent v2.0 — 简历描述 & 面试准备

## 简历项目描述

**项目描述**：从零构建的三层架构 AI Coding Agent 系统，采用 Provider → Agent Loop → Application 严格分层设计，零 LangChain/LangGraph 依赖，仅依赖 httpx + pydantic + typer + rich。实现了双循环架构（外层 Workflow 状态机 + 内层 Agent Loop）、三层事件驱动、工具调用统一兜底、三级上下文压缩、权限管控等完整工程闭环。

**主要贡献与核心设计**：

### 1. 三层架构设计

自底向上拆分为 `dot_ai`（Provider 抽象，仅 httpx）→ `dot_agent`（Agent 核心，pydantic）→ `dot_coding`（应用层，typer/rich/mcp），层间依赖严格单向，消除循环依赖。用 `Protocol` 定义 `ModelProvider` 单方法接口，新增 Provider 只需实现一个类，解耦了 LLM 调用与业务逻辑。

### 2. 双循环架构替代 LangGraph

外层 Workflow 用 `WorkflowPhase` 枚举 + `if/else` 显式状态机（plan → code → validate）替代 LangGraph StateGraph，内层 Agent Loop 实现 LLM 流式响应 → 工具执行 → 结果追加的循环。`AgentLoopResult` 通过函数返回值传递，不暴露内部状态，解决了 LangGraph 节点通过共享 Session mutation 通信的问题。

### 3. 轻量工具系统

`AgentTool` 用 `frozen dataclass + callable` 替代 LangChain `StructuredTool` 类继承，工具定义从 ~50 行降到 ~10 行。`execute_tool_safely` 统一兜底层覆盖工具不存在、参数错误、超时（30s）、执行异常、MCP 断连等 7 种失败场景，所有错误返回 LLM 可操作的提示，支持模型自动重试。

### 4. 三级上下文压缩

L1（≥50%）去除可恢复的 tool 结果（如 read_file），保留路径引导 LLM 重读，零 LLM 开销；L2（≥70%）删除老旧工具调用输出，保留最近 N 轮；L3（≥85%）调用 LLM 生成结构化摘要。上下文估算采用 Provider 锚定策略——以 LLM 返回的 `usage.total_tokens` 为权威值，对增量消息做字符估算（4 chars/token），避免了纯字符估算的累积误差。

### 5. 事件驱动架构

定义三层事件 `ProviderEvent`（LLM 流式 delta）→ `AgentEvent`（Agent 生命周期）→ `CodingEvent`（会话级），均为 Pydantic discriminated union。`TraceCollector` 订阅 AgentEvent 流自动生成 span 树，替代手动埋点；`AgentHarness` 的 `_notify` 拷贝监听器列表再迭代，防止回调中取消订阅的"迭代中修改集合"异常；事件携带 `model_copy(deep=True)` 快照防止引用别名 bug。

### 6. 权限管控与安全

三级拦截（系统黑名单 → 项目黑名单 → 模式规则），决策三态 ALLOW/ASK/DENY，权限检查在 hooks 之前且不可被用户扩展。`bash` 工具内置危险命令正则检测（`rm -rf`、fork bomb 等），路径校验用 `Path.is_relative_to()` 防遍历。ASK 无 UI 时自动降级 DENY（无头兜底不卡死）。

### 7. 可靠性机制

`repair_tool_history` 每次发请求前自愈修复孤儿 result、重复 result、错位 result；中断时自动为缺失的 tool result 合成 `is_error=True` 的错误消息；`ExtensionGeneration` liveness token 确保热重载后旧引用立即失效，防止幽灵注册。

---

## 面试官拷打 Q&A

### Q1：为什么要拆三层？直接一个包不行吗？

**答**：一个包的问题是循环依赖和职责不清。比如 Provider 层只关心 HTTP 调用和 SSE 解析，不应该依赖 pydantic 的 AgentTool；Agent 层只关心消息循环和工具执行，不应该知道 typer/rich 的存在。拆三层后，每层可以独立测试——mock 一个 `ModelProvider` 就能测 Agent Loop，不需要真实 LLM。

### Q2：为什么不用 LangGraph？它不是挺好的吗？

**答**：LangGraph 的 StateGraph 解决的是"图编排"问题，但我们实际只用了 3 个节点（plan → code → validate）+ 条件边，没有用到 interrupt/checkpointer/rollback 等高级功能。相当于用了一个重型框架当 if/else 路由器。自定义状态机用 `WorkflowPhase` 枚举 + 函数返回值，状态转换一目了然，不需要理解图的节点/边/状态通道概念。

### Q3：AgentTool 用 frozen dataclass 有什么好处？

**答**：三个好处。一是 `frozen=True` 防止注册后被意外修改，工具定义是契约不应该变；二是 `slots=True` 减少内存占用，Agent 可能注册几十个工具；三是零继承，不需要理解 LangChain 的 `StructuredTool`/`BaseTool`/`BaseModel` 继承链，一个 dataclass + callable 就够了。

### Q4：三级压缩为什么分三级？一级不行吗？

**答**：一级压缩的问题是"要么太轻要么太重"。L1 只删可恢复的 tool 结果（比如 read_file 的输出），因为文件在磁盘上，LLM 需要时可以重读，这一步零 LLM 开销；L2 删老旧的 bash/grep 输出，这些不可恢复但也不重要了；L3 才调 LLM 做摘要，因为摘要本身有信息损失，只在快溢出时才用。三级递进，尽量用便宜的方式延后昂贵的 LLM 调用。

### Q5：Provider 锚定的上下文估算是什么意思？

**答**：LLM 每次响应都会返回 `usage.total_tokens`，这是准确值。但我们还有新增的消息（用户刚输入的、工具刚执行完的）没有被报告过。所以估算公式是：`total = provider_reported_tokens + estimate(new_messages)`。新消息用字符数 / 4 估算。这比纯字符估算准确，因为已知部分是精确的。

### Q6：repair_tool_history 修什么？

**答**：三种问题。一是"孤儿 result"——有 ToolResultMessage 但没有对应的 AssistantMessage 中的 ToolCall（可能是会话文件被手动编辑了）；二是"重复 result"——同一个 tool_call_id 有两个 result；三是"错位 result"——result 出现在对应的 assistant message 之前。每次发请求前自动修复，保证发给 LLM 的消息历史是完整的。

### Q7：权限系统为什么不用 hook？

**答**：Hook 是给用户扩展用的生命周期回调（比如在工具执行前后做审计），权限是核心安全管控。如果权限用 hook 实现，用户可以通过 hook 覆盖权限决策，这不安全。所以权限检查在 hooks 之前执行，且不可被用户扩展。系统黑名单即使人工确认也不可放行（比如 `rm -rf /`）。

### Q8：事件驱动追踪比手动埋点好在哪？

**答**：手动埋点需要在每个节点/工具调用处加 `start_span()`/`span.finish()`，代码散落在各模块，新增功能要同步加 span。事件驱动是 TraceCollector 订阅 AgentEvent 流，Agent Loop 自动发射事件（TurnStart/End、ToolExecutionStart/End），TraceCollector 根据事件层级自动推栈生成 span 树。新增事件类型时，TraceCollector 自动追踪，零侵入。

### Q9：ExtensionGeneration liveness token 解决什么问题？

**答**：热重载（`/reload`）后，旧的扩展引用可能还在被异步任务持有。如果不处理，旧引用调用 `register_tool()` 会注册到新 generation 里，造成幽灵注册。ExtensionGeneration 每次 reload 创建新 token，旧 token 的所有操作（包括看似无害的只读属性访问）立即抛出 `ExtensionError`，fail-fast 而不是静默失败。

### Q10：ASK 无 UI 时为什么降级 DENY 而不是阻塞等待？

**答**：在 headless 环境（CI/CD、脚本调用）中没有用户可以点确认，如果阻塞等待会永远卡死。降级 DENY 是安全的选择——宁可拒绝一个合法操作（用户可以手动重试），也不要卡死整个 Agent。这比默认 ALLOW 安全得多。

### Q11：双队列消息（steering / follow-up）解决什么问题？

**答**：用户在 Agent 运行时发送消息，语义上有两种：一是"停下手头的事，先做这个"（steering，比如"别改那个文件了"）；二是"做完之后再做这个"（follow-up，比如"然后帮我写个测试"）。如果不区分，用户的纠错消息要等当前工具执行完才能被处理，体验很差。steering 在下一轮开始前立即插入，follow-up 在所有工具执行完后才消费。

### Q12：为什么用 Protocol 而不是 ABC？

**答**：ABC 要求继承，Provider 实现必须 `class MyProvider(BaseProvider)`，这引入了耦合。Protocol 是结构化子类型（鸭子类型），只要实现了 `stream_response` 方法就行，不需要继承任何类。这样第三方 Provider 可以零侵入地接入，不需要依赖我们的包。
