# MokioClaw vs Claude Code / Codex：差距分析与改进路线

> 文档目的：对照 Claude Code、Codex 这类正式 Agent 实现，识别 MokioClaw 当前的优势、差距和改进机会。

## 📊 执行摘要

MokioClaw 已经是一个**相当完善**的 Mini CodeAgent 实现，具备：

- ✅ MultiAgent 编排（planner/searchAgent/codeAgent）
- ✅ LangGraph 状态机工作流
- ✅ Context Engineering（三层记忆 + 自动压缩）
- ✅ Harness Engineering（审批/checkpoint/trace）
- ✅ Textual TUI 交互层

**但**对照 Claude Code / Codex 这类生产级 Agent，在**工具设计质量、上下文管理精度、安全性、可靠性、性能**五个维度仍有明显差距。

---

## 🎯 当前优势

### 1. 架构设计优秀
- **分层清晰**：CLI → Core → Graph → Agents → Tools 五层分离
- **状态管理完善**：RuntimeState + MokioGraphState 双状态体系
- **事件驱动**：所有节点交互通过结构化事件流，支持 Rich 和 TUI 双渲染

### 2. 工程化能力领先
- **Checkpoint/Resume**：light/strict 双模式，支持中断恢复
- **Trace 链路追踪**：events.jsonl + summary.json + timeline.md
- **审批系统**：inline/auto/deny 三模式，风险命令分类
- **Workspace 隔离**：每个任务独立 workspace，文件快照

### 3. 教学友好
- 文档详尽（project-overview.md、workspace-lifecycle.md、视频文稿）
- 代码注释丰富
- 分阶段演进路径清晰（6 个阶段）

---

## 📉 关键差距

### 维度 1：工具调用质量 ⭐⭐⭐⭐⭐

#### Claude Code 的特点：
- **工具 Schema 极其精细**：每个工具都有严格的 JSON Schema，包含详细的 `description`、`parameters`、`examples`
- **工具描述即 Prompt**：Claude Code 的工具描述本身就是精心设计的 prompt engineering
- **输入验证严格**：工具执行前会验证参数合法性
- **输出格式强制**：要求工具返回特定格式，便于解析

#### MokioClaw 当前问题：
```python
# ❌ 当前：工具描述过于简略
def bash_tool_description() -> str:
    return "Run a safe development shell command inside the workspace..."

# ✅ Claude Code 风格：超详细描述
"""
Execute a shell command inside the workspace.

**Platform**: Windows (cmd.exe)

**Syntax examples**:
- List files: dir
- Read file: type file.txt
- Check file exists: if exist file.txt (echo exists)
- Chain commands: command1 && command2

**Safety**:
- Never run: rm -rf, format, shutdown, del /s /q C:\\
- High-risk commands (pip install, npm install) require approval
- Timeout: 120s default, 600s max

**Output**:
- stdout/stderr captured (max 6000 chars)
- Long output saved to .mokioclaw/bash-outputs/
- Background mode: run_in_background=true
"""
```

#### 改进建议：
1. **为每个工具重写 description**，包含：
   - 当前平台说明
   - 语法示例（3-5 个）
   - 安全警告（禁止的命令）
   - 输出格式说明
   - 常见用例

2. **添加工具级输入验证**：
   ```python
   def _validate_bash_args(command: str, timeout_seconds: int | None) -> list[str]:
       errors = []
       if not command.strip():
           errors.append("command cannot be empty")
       if timeout_seconds and (timeout_seconds < 1 or timeout_seconds > 600):
           errors.append(f"timeout must be 1-600s, got {timeout_seconds}")
       return errors
   ```

3. **工具返回结构化错误**：
   ```python
   # ❌ 当前：简单返回字符串
   return {"ok": False, "error": "command not found"}

   # ✅ Claude Code 风格：结构化错误
   return {
       "ok": False,
       "error": "command_not_found",
       "error_message": "Command 'git' not found in PATH",
       "hint": "Install git or check PATH configuration",
       "tool": "BashTool",
       "command": "git status"
   }
   ```

---

### 维度 2：上下文管理精度 ⭐⭐⭐⭐⭐

#### Claude Code 的特点：
- **动态 token 预算**：根据模型类型和任务复杂度动态调整
- **智能优先级压缩**：保留"用户指令 + 最近错误 + 关键文件路径"，压缩中间过程
- **Context window 预警**：接近上限时提前提醒用户
- **自动分支切换**：检测到 git branch 切换时自动更新上下文

#### MokioClaw 当前问题：

**问题 1：压缩策略过于粗糙**
```python
# ❌ 当前：固定阈值 400K，不分任务类型
DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

# ✅ Claude Code 风格：根据任务类型调整
CONTEXT_LIMITS = {
    "simple_task": 100_000,   # 单文件修改
    "medium_task": 200_000,   # 多文件重构
    "complex_task": 400_000,  # 大型功能开发
}
```

**问题 2：压缩时"一刀切"**
```python
# ❌ 当前：所有消息同等对待
messages = state.get("messages", [])
# 压缩所有消息...

# ✅ Claude Code 风格：分级保留
KEEP_ALWAYS = [
    "用户原始任务",
    "最近 3 轮对话",
    "失败原因和下一步",
    "当前文件和光标位置",
]
COMPRESS_HEAVILY = [
    "BashTool 长输出（只保留最后 10 行）",
    "中间过程的 tool call",
    "旧的 plan_snapshot",
]
```

**问题 3：缺少 token 预算分配**
```python
# ❌ 当前：没有预算分配
def context_monitor_node(state):
    token_count = estimate_context_tokens(state)
    should_compress = token_count > limit
    return {"context_token_count": token_count, ...}

# ✅ Claude Code 风格：多级预算
TOKEN_BUDGET = {
    "system_prompt": 5_000,      # 工具描述 + 规则
    "user_task": 1_000,          # 用户指令
    "working_memory": 10_000,    # 当前计划、todos
    "recent_messages": 50_000,   # 最近对话
    "tool_outputs": 30_000,      # 工具输出
    "reserved": 50_000,          # 留给模型生成
}
```

#### 改进建议：
1. **实现智能压缩策略**：
   - 分级保留：Always Keep → Compress Lightly → Compress Heavily → Drop
   - 保留"错误信息"和"关键决策"，压缩"成功的中间步骤"

2. **动态 token 预算**：
   - 根据任务复杂度（工具调用次数、文件修改数）调整阈值
   - 预留足够空间给模型输出

3. **Context 使用可视化**：
   ```
   📊 Context: 78,432 / 200,000 tokens
   ├─ System: 4,892 (2%)
   ├─ Task: 843 (0%)
   ├─ Memory: 12,304 (6%)
   ├─ Messages: 45,230 (23%)
   ├─ Tools: 15,143 (8%)
   └─ Reserved: 120,000 (60%)
   ```

---

### 维度 3：安全性 ⭐⭐⭐⭐⭐

#### Claude Code 的特点：
- **Sandbox 执行环境**：危险命令在隔离环境执行
- **白名单机制**：只允许访问明确授权的目录
- **工具权限分级**：read-only / write / execute 三级权限
- **自动回滚**：工具调用失败时自动恢复

#### MokioClaw 当前问题：

**问题 1：路径安全检查不够严格**
```python
# ❌ 当前：只检查是否在 workspace 内
def assert_workspace_path(self, path: Path) -> Path:
    resolved = path.resolve()
    workspace = self.workspace.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"path must stay inside workspace: {workspace}")
    return resolved

# ✅ Claude Code 风格：白名单 + 黑名单
ALLOWED_DIRS = ["src/", "tests/", "docs/"]
BLOCKED_DIRS = [".git/", "node_modules/", ".venv/", "__pycache__/"]

def assert_safe_path(self, path: Path, operation: str) -> Path:
    resolved = path.resolve()
    # 1. 必须在 workspace 内
    if workspace not in resolved.parents:
        raise PathTraversalError(...)
    # 2. 不能在黑名单目录
    if any(str(resolved).startswith(workspace / d) for d in BLOCKED_DIRS):
        raise AccessDeniedError(...)
    # 3. 写操作检查白名单
    if operation == "write" and not any(...):
        raise AccessDeniedError(...)
    return resolved
```

**问题 2：BashTool 命令注入风险**
```python
# ❌ 当前：直接拼接命令
completed = subprocess.run(
    command,
    cwd=state.workspace,
    shell=True,  # ← 有注入风险！
    ...
)

# ✅ Claude Code 风格：参数化执行
def run_bash_safe(command: str, args: list[str]) -> dict:
    # 1. 解析命令，提取可执行文件 + 参数
    executable, *args = shlex.split(command)
    # 2. 检查可执行文件在白名单
    if executable not in ALLOWED_COMMANDS:
        raise CommandNotAllowedError(executable)
    # 3. 参数化执行（不经过 shell）
    completed = subprocess.run(
        [executable] + args,
        cwd=state.workspace,
        shell=False,  # ← 安全！
        ...
    )
```

**问题 3：缺少工具调用审计日志**
```python
# ❌ 当前：只有 trace 记录，没有审计
trace.record_custom_event(event)

# ✅ Claude Code 风格：详细审计
audit_log = {
    "timestamp": datetime.utcnow().isoformat(),
    "tool": "BashTool",
    "command": command,
    "args": args,
    "cwd": str(state.workspace),
    "user": os.getenv("USER"),
    "session_id": state.session_id,
    "approval": approval_decision,  # 如果有
    "result": {"ok": True, "exit_code": 0},
    "duration_ms": elapsed,
    "risk_level": risk_level,
}
audit_logger.info(audit_log)
```

#### 改进建议：
1. **实现路径白名单/黑名单机制**
2. **BashTool 使用参数化执行**（`shell=False`）
3. **添加工具调用审计日志**
4. **实现 Sandbox 模式**（受限环境执行）

---

### 维度 4：可靠性 ⭐⭐⭐⭐

#### Claude Code 的特点：
- **工具调用重试**：失败时自动重试（最多 3 次）
- **优雅降级**：模型调用失败时 fallback 到规则引擎
- **超时控制**：每个节点都有独立超时
- **状态持久化**：高频 checkpoint（每 10 秒）

#### MokioClaw 当前问题：

**问题 1：缺少工具调用重试**
```python
# ❌ 当前：调用失败直接放弃
result = tool.invoke(tool_input)
if not result.get("ok"):
    return result

# ✅ Claude Code 风格：指数退避重试
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def invoke_tool_with_retry(tool, tool_input):
    result = tool.invoke(tool_input)
    if not result.get("ok") and result.get("retryable"):
        raise TemporaryError(result["error"])
    return result
```

**问题 2：模型调用没有降级机制**
```python
# ❌ 当前：模型调用失败直接抛出
response = create_model().invoke(messages)

# ✅ Claude Code 风格：fallback 策略
def invoke_with_fallback(messages, fallback_prompt=None):
    try:
        return create_model().invoke(messages)
    except RateLimitError:
        logger.warning("rate limited, using fallback")
        if fallback_prompt:
            return rule_based_fallback(messages)
        raise
    except Exception as exc:
        logger.error(f"model call failed: {exc}")
        return {"content": "I encountered an error. Please try again."}
```

**问题 3：Checkpoint 频率不够**
```python
# ❌ 当前：只在关键节点保存
manager.save(current_state, status="running", latest_node=latest_node)

# ✅ Claude Code 风格：高频 checkpoint
def save_checkpoint_throttled(self, state, *, status="running", latest_node=None):
    now = time.time()
    if now - self.last_checkpoint_time < 10:  # 10 秒内不重复保存
        return None
    self.last_checkpoint_time = now
    return self.save(state, status=status, latest_node=latest_node)
```

#### 改进建议：
1. **为工具调用添加重试机制**（指数退避）
2. **模型调用添加 fallback 策略**
3. **提高 checkpoint 频率**（时间节流）
4. **添加健康检查端点**（监控模型/API 状态）

---

### 维度 5：性能 ⭐⭐⭐⭐

#### Claude Code 的特点：
- **并行工具调用**：多个独立工具同时执行
- **流式响应**：逐步输出，不等完整结果
- **缓存机制**：相似任务的结果缓存
- **增量加载**：大文件只读取需要部分

#### MokioClaw 当前问题：

**问题 1：工具调用完全串行**
```python
# ❌ 当前：串行调用
for call in tool_calls:
    result = execute_tool(call)
    messages.append(ToolMessage(...))

# ✅ Claude Code 风格：并行调用
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(execute_tool, call) for call in independent_calls]
    results = [f.result() for f in futures]
```

**问题 2：文件读取无增量加载**
```python
# ❌ 当前：一次性读取完整文件
content = path.read_text(encoding="utf-8")

# ✅ Claude Code 风格：按需读取
def read_file_incremental(path: Path, offset: int = 0, limit: int = 2000) -> str:
    """只读取需要的部分，大文件支持分页"""
    total_lines = sum(1 for _ in path.open())
    if offset + limit >= total_lines:
        # 读取末尾部分（可能是关键代码）
        ...
```

**问题 3：缺少缓存机制**
```python
# ❌ 当前：每次重新计算
token_count = estimate_context_tokens(state)  # 每次都调用模型

# ✅ Claude Code 风格：缓存计算结果
from functools import lru_cache

@lru_cache(maxsize=128)
def estimate_tokens_cached(text: str, model: str = "default") -> int:
    return model.get_num_tokens(text)
```

#### 改进建议：
1. **实现并行工具调用**（识别独立调用）
2. **文件读取支持分页**（offset/limit 已有，可优化大文件）
3. **添加缓存层**（token 估算、文件读取、搜索结果）
4. **流式输出优化**（使用 LangChain 的 streaming）

---

### 维度 6：用户体验 ⭐⭐⭐⭐

#### Claude Code 的特点：
- **实时进度指示**： spinner + 进度百分比
- **彩色输出**：不同工具/节点用不同颜色
- **快捷键支持**：Ctrl+C 取消、Ctrl+D 新任务
- **清晰的错误提示**：包含"如何修复"建议

#### MokioClaw 当前问题：

**问题 1：进度指示不够精细**
```python
# ❌ 当前：简单文字
writer({"type": "tool_call", "name": "BashTool", "args": {...}})

# ✅ Claude Code 风格：带 spinner 的进度
with Progress() as progress:
    task = progress.add_task(f"[cyan]Running {tool_name}...", total=100)
    result = execute_tool(call)
    progress.update(task, advance=100, description=f"[green]✓ {tool_name}")
```

**问题 2：错误提示不够友好**
```python
# ❌ 当前：简单错误
return {"ok": False, "error": "file not found"}

# ✅ Claude Code 风格：结构化错误 + 修复建议
return {
    "ok": False,
    "error": "FileNotFoundError",
    "error_message": "Cannot read file: ami\


n.py",
    "file": "aminn.py",
    "cwd": str(state.workspace),
    "hint": "Check if the file exists or if you have the correct filename",
    "suggestion": "Use FileWriteTool to create the file first",
    "related_files": ["main.py", "utils.py"],  # 可能相关的文件
}
```

**问题 3：缺少快捷键和交互优化**
```python
# ❌ 当前：只有基础的 Ctrl+C 处理
# ✅ Claude Code 风格：丰富的交互
- Ctrl+C: 取消当前任务
- Ctrl+D: 开启新任务
- Ctrl+R: 恢复最近任务
- Ctrl+L: 清空屏幕
- Ctrl+P: 上一个任务
- Tab: 自动补全文件路径
```

#### 改进建议：
1. **添加 Progress 组件**（Rich/Textual 的 Progress bar）
2. **增强错误提示**（包含 hint 和 suggestion）
3. **实现快捷键绑定**（TUI 模式）
4. **添加命令历史**（上下箭头浏览）

---

## 🗺️ 改进路线图

### Phase 1：工具质量提升（2-3 天）

**目标**：让工具调用更可靠、更易用

**任务清单**：
- [ ] 重写所有工具的 `description`，添加平台说明、语法示例、安全警告
- [ ] 为每个工具添加输入验证函数
- [ ] 统一工具返回格式（结构化错误）
- [ ] 为工具调用添加 `retry` 装饰器（指数退避）

**优先级工具**：
1. **BashTool**（最复杂，影响最大）
2. **FileWriteTool**（最常用）
3. **FileEditTool**（最容易出错）

---

### Phase 2：上下文管理优化（3-4 天）

**目标**：更智能的压缩策略，更高的 token 利用率

**任务清单**：
- [ ] 实现分级压缩策略（Always Keep / Light / Heavy / Drop）
- [ ] 添加动态 token 预算（根据任务复杂度调整）
- [ ] 实现 Context 使用可视化面板（TUI）
- [ ] 优化压缩 prompt（更精准的指令）
- [ ] 添加 token 使用预测（提前预警）

---

### Phase 3：安全加固（2-3 天）

**目标**：更严格的安全机制，防止误操作和恶意输入

**任务清单**：
- [ ] 实现路径白名单/黑名单机制
- [ ] BashTool 改为参数化执行（`shell=False`）
- [ ] 添加工具调用审计日志
- [ ] 实现 Sandbox 模式（受限环境）
- [ ] 添加文件修改确认（重要文件删除前二次确认）

---

### Phase 4：可靠性增强（2-3 天）

**目标**：更高的容错能力，更好的恢复机制

**任务清单**：
- [ ] 为工具调用添加重试机制
- [ ] 模型调用添加 fallback 策略
- [ ] 提高 checkpoint 频率（时间节流）
- [ ] 添加健康检查端点
- [ ] 实现优雅降级（模型不可用时提示用户）

---

### Phase 5：性能优化（2-3 天）

**目标**：更快的响应速度，更低的 token 消耗

**任务清单**：
- [ ] 实现并行工具调用
- [ ] 添加缓存层（token 估算、文件读取）
- [ ] 优化大文件读取（分页 + 增量）
- [ ] 流式响应优化
- [ ] 添加性能监控（耗时统计）

---

### Phase 6：用户体验打磨（3-4 天）

**目标**：更友好的交互，更清晰的输出

**任务清单**：
- [ ] 添加 Progress 组件（Rich/Textual）
- [ ] 增强错误提示（hint + suggestion）
- [ ] 实现快捷键绑定
- [ ] 添加命令历史
- [ ] 优化颜色方案（区分不同节点/工具）
- [ ] 添加"思考中"动画

---

## 🎯 优先级矩阵

| 改进项 | 影响力 | 复杂度 | 优先级 |
|--------|--------|--------|--------|
| 工具 description 重写 | ⭐⭐⭐⭐⭐ | 低 | **P0** |
| 工具输入验证 | ⭐⭐⭐⭐⭐ | 低 | **P0** |
| 分级压缩策略 | ⭐⭐⭐⭐⭐ | 中 | **P0** |
| 路径白名单/黑名单 | ⭐⭐⭐⭐ | 中 | **P1** |
| 工具调用重试 | ⭐⭐⭐⭐ | 中 | **P1** |
| 并行工具调用 | ⭐⭐⭐⭐ | 中 | **P1** |
| 审计日志 | ⭐⭐⭐ | 低 | **P1** |
| TUI Progress 组件 | ⭐⭐⭐ | 低 | **P2** |
| 缓存机制 | ⭐⭐⭐ | 高 | **P2** |
| 快捷键支持 | ⭐⭐⭐ | 低 | **P2** |

---

## 📈 成功指标

改进后应该达到的效果：

1. **工具调用成功率**：从 85% → 95%+
2. **Token 利用率**：压缩后上下文减少 40%+
3. **安全性**：路径遍历攻击 100% 拦截
4. **可靠性**：任务完成率从 70% → 90%+
5. **响应速度**：工具调用平均耗时减少 30%+

---

## 🚀 快速开始（下一步行动）

**建议立即开始**：
1. 重写 BashTool 的 description（作为模板）
2. 实现分级压缩策略
3. 添加路径白名单/黑名单

**本周目标**：完成 Phase 1 + Phase 3

---

## 📚 参考资料

- [Claude Code 官方文档](https://docs.anthropic.com/en/docs/claude-code)
- [Codex CLI 开源实现](https://github.com/openai/codex)
- [LangGraph 最佳实践](https://langchain-ai.github.io/langgraph/concepts/)
- [Anthropic Prompt Engineering](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering)
