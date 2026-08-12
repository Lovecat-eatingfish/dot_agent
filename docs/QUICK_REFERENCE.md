# MokioClaw 改进快速参考

> 对照 Claude Code / Codex 实现的生产级改进速查手册

## 🎯 核心改进一览

### 1. 工具质量（Tool Quality）

#### BashTool Description 重写

**位置**：`src/mokioclaw/tools/bash_tool.py::bash_tool_description()`

**关键改进**：
- ✅ 从 1 句话 → 200+ 字详细说明
- ✅ 包含平台特定语法示例（Windows cmd / POSIX）
- ✅ 正面/反面示例对比（✅ Right / ❌ Wrong）
- ✅ 输出字段说明
- ✅ 安全警告

**使用模板**：
```python
def tool_description() -> str:
    return """Execute {action}.

**Platform**: {platform}

**Key behaviors**:
- {behavior_1}
- {behavior_2}

**Common use cases**:
- {use_case_1}
- {use_case_2}

**Output format**:
- ok: boolean
- {other_fields}

**Security**:
- {security_note}
"""
```

#### 工具输入验证

**位置**：每个工具的 `_validate_*_args()` 函数

**示例（BashTool）**：
```python
def _validate_bash_args(command: str, timeout_seconds: int | None = None) -> list[str]:
    errors = []
    if not command.strip():
        errors.append("Command must not be empty")
    if len(command) > 10000:
        errors.append(f"Command too long ({len(command)} chars), max 10000")
    if timeout_seconds and (timeout_seconds < 1 or timeout_seconds > 600):
        errors.append(f"timeout_seconds must be 1-600s, got {timeout_seconds}")
    return errors
```

**已在 BashTool、FileWriteTool、FileEditTool 实现**

#### 结构化错误返回

**统一格式**：
```python
{
    "ok": False,
    "error": "error_code",           # 错误代码（用于程序判断）
    "error_message": "...",          # 人类可读信息
    "tool": "ToolName",              # 工具名称
    "hint": "How to fix it",         # 修复建议
    # 其他上下文字段...
}
```

---

### 2. 上下文管理（Context Management）

#### 分级压缩策略

**位置**：`src/mokioclaw/memory/tiered_compression.py`

**四级策略**：
```python
# 优先级定义
KEEP_ALWAYS_PRIORITY = 100      # 永远保留
COMPRESS_LIGHTLY_PRIORITY = 50  # 轻度压缩（截断）
COMPRESS_HEAVILY_PRIORITY = 20  # 重度压缩（摘要）
DROP_PRIORITY = 0              # 直接删除
```

**使用示例**：
```python
from mokioclaw.memory.tiered_compression import compress_messages_by_tier

compressed = compress_messages_by_tier(
    messages,
    context_summary=state.get("context_summary", ""),
)
```

**自动规则**：
- SystemMessage（含工具描述）→ KEEP_ALWAYS
- HumanMessage（用户指令）→ KEEP_ALWAYS
- BashTool 输出 > 2000 chars → COMPRESS_HEAVILY
- FileReadTool 输出 > 3000 chars → COMPRESS_HEAVILY
- 空消息 → DROP

**预估压缩效果**：
```python
from mokioclaw.memory.tiered_compression import estimate_tokens_for_tiered_compression

stats = estimate_tokens_for_tiered_compression(messages)
# {
#     "original_tokens": 50000,
#     "compressed_tokens": 20000,
#     "reduction_tokens": 30000,
#     "reduction_pct": 60.0,
# }
```

---

### 3. 安全性（Security）

#### 路径白名单/黑名单

**位置**：`src/mokioclaw/security/path_security.py`

**默认配置**：
```python
# 黑名单（禁止访问）
BLACKLISTED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mokioclaw", ...}

# 写操作白名单
ALLOWED_WRITE_DIRS = {"src", "tests", "docs", "examples", "scripts", ...}

# 敏感文件模式
SENSITIVE_PATTERNS = [r".*\.env$", r".*\.pem$", r".*\.key$", r"id_rsa.*", ...]
```

**使用示例**：
```python
from mokioclaw.security.path_security import validate_path_access, PathAccessDeniedError

try:
    safe_path = validate_path_access(state, path, operation="write")
except PathAccessDeniedError as exc:
    return {"ok": False, "error": str(exc)}
```

**安全异常**：
- `PathTraversalError` - 路径遍历攻击
- `PathAccessDeniedError` - 黑名单/写权限拒绝

---

### 4. 可靠性（Reliability）

#### 工具调用重试

**位置**：`src/mokioclaw/reliability/retry.py`

**装饰器用法**：
```python
from mokioclaw.reliability.retry import retry_on_failure, RetryableError

@retry_on_failure(
    max_attempts=3,
    initial_delay=1.0,
    max_delay=10.0,
    exponential_base=2.0,
)
def my_tool(...):
    ...
```

**可重试异常**：
```python
RETRYABLE_ERRORS = [
    "timeout",                # 超时
    "network_error",          # 网络错误
    "rate_limited",           # 限流
    "temporary_failure",      # 临时失败
]
```

**函数式重试**：
```python
from mokioclaw.reliability.retry import invoke_tool_with_retry

result = invoke_tool_with_retry(
    tool_func=my_tool,
    tool_input={"arg1": "value"},
    max_attempts=3,
)
```

**退避策略**：
- 第 1 次失败 → 等待 1s
- 第 2 次失败 → 等待 2s
- 第 3 次失败 → 等待 4s
- 最大延迟 10s

---

### 5. 性能（Performance）

#### 并行工具调用

**位置**：`src/mokioclaw/reliability/parallel.py`

**自动检测**：
```python
from mokioclaw.reliability.parallel import are_tools_independent

# 可以并行的情况：
# - 多个 FileReadTool（读不同文件）
# - 多个 GrepTool（搜索不同内容）
# - 多个 WebSearchTool

# 不能并行的情况：
# - 混合读写操作
# - 读写同一个文件
# - 写操作之间
```

**使用示例**：
```python
from mokioclaw.reliability.parallel import execute_tools_in_parallel

results = execute_tools_in_parallel(
    tool_calls=[
        {"name": "FileReadTool", "args": {"file_path": "a.py"}},
        {"name": "FileReadTool", "args": {"file_path": "b.py"}},
        {"name": "GrepTool", "args": {"pattern": "foo"}},
    ],
    execute_tool_func=lambda tc: tool.invoke(tc["args"]),
    max_workers=4,
)
```

**异步版本**：
```python
from mokioclaw.reliability.parallel import execute_tools_in_parallel_async

results = await execute_tools_in_parallel_async(
    tool_calls,
    execute_tool_func=async_tool_func,
    max_concurrency=4,
)
```

---

## 📁 新增模块索引

| 模块 | 路径 | 用途 |
|------|------|------|
| `tiered_compression` | `src/mokioclaw/memory/tiered_compression.py` | 分级压缩策略 |
| `path_security` | `src/mokioclaw/security/path_security.py` | 路径安全控制 |
| `retry` | `src/mokioclaw/reliability/retry.py` | 工具调用重试 |
| `parallel` | `src/mokioclaw/reliability/parallel.py` | 并行工具调用 |

---

## 🔄 向后兼容性

所有改进都**向后兼容**：

1. **工具描述**：只影响 LLM 理解，不影响工具行为
2. **输入验证**：验证失败返回结构化错误，不影响成功调用
3. **分级压缩**：在 LLM 压缩基础上增强，不破坏现有逻辑
4. **路径安全**：增强检查，不改变现有 API
5. **重试机制**：可选装饰器，不强制所有工具使用
6. **并行调用**：自动检测，不改变调用接口

---

## 🧪 测试

运行测试：
```bash
uv run pytest tests/ -v
```

重点测试文件：
- `tests/test_tools.py` - 工具测试（注意 Windows 兼容性问题）
- `tests/test_graph.py` - Graph 工作流测试
- `tests/test_formatter.py` - 格式化测试

**已知问题**：
- `test_bash_env_file_expands_existing_variables` - Windows 兼容性
- `test_bash_tool_description_mentions_windows_cmd` - description 已更新，需调整测试期望

---

## 📚 更多文档

- [Claude Code 对比分析](claude-code-comparison-analysis.md) - 详细的差距分析
- [改进总结](IMPROVEMENTS.md) - 完整的改进记录
- [项目全景](docs/project-overview.md) - 项目架构和设计
- [Workspace 生命周期](docs/workspace-lifecycle.md) - 运行链路详解

---

**版本**：v2.0 - 对照 Claude Code / Codex 改进版
**日期**：2026-08-10
**状态**：✅ Phase 1-5 完成，Phase 6-7 可选
