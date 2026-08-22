# 链路追踪设计方案
### 1. 存储格式：优先用 JSON Lines

每行一个独立 JSON 对象，比纯文本易解析，比整文件 JSON 容错性高（程序异常退出也不会损坏整个文件）；后续想导入 Jaeger/Langfuse，只需写简单转换脚本，无需重构埋点。

文件命名建议：`./dot/traces/2026-08-22/trace_{session_id}.jsonl`，按天 + 会话拆分，方便定向查找。

### 2. 单条 Span 标准字段（对齐 OTel 语义，后续无缝升级）

字段设计直接兼容 OpenTelemetry 标准，后续升级生产级方案时埋点逻辑完全复用：

```
{
  "trace_id": "全局唯一，贯穿整条请求全链路",
  "span_id": "当前节点唯一ID",
  "parent_span_id": "父节点ID，用于还原调用层级",
  "timestamp": "ISO8601 毫秒级时间戳",
  "duration_ms": "执行耗时（毫秒）",
  "service": "模块分类：agent_host/graph_node/mcp/llm/session",
  "name": "操作名称：plan_node/call_mcp_tool/llm_inference",
  "status": "ok / error",
  "tags": { "session_id": "xxx", "model": "gpt-4o", "tool_name": "read_file" },
  "input_summary": "入参摘要（建议截断，避免文件膨胀）",
  "output_summary": "返回结果摘要",
  "error_stack": "异常场景填充完整堆栈"
}
```

### 3. 写入方式：异步队列写入，禁止同步 IO 阻塞主链路

用`queue.Queue` + 后台守护线程实现，避免文件 IO 拖慢 Agent 执行速度，极简可复用实现：

```
import json
import queue
import threading
import time
from pathlib import Path

class LocalFileTraceExporter:
    def __init__(self, trace_dir: str = "./traces"):
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._queue = queue.Queue()
        # 后台守护线程，程序退出自动销毁
        self._worker = threading.Thread(target=self._write_loop, daemon=True)
        self._worker.start()

    def export(self, span: dict):
        """对外暴露的上报接口，非阻塞"""
        self._queue.put(span)

    def _write_loop(self):
        while True:
            span = self._queue.get()
            try:
                session_id = span.get("tags", {}).get("session_id", "default")
                date_str = time.strftime("%Y-%m-%d")
                file_path = self.trace_dir / date_str / f"trace_{session_id}.jsonl"
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(span, ensure_ascii=False) + "\n")
            except Exception:
                pass  # 追踪失败绝对不能影响主业务流程
            finally:
                self._queue.task_done()
```

### 4. 架构分层：埋点与存储解耦

定义统一的`Tracer`抽象接口，本地文件只是其中一种实现类。后续替换为 OTel/Langfuse 时，只更换 Exporter 实现，业务层埋点代码一行不用改。

## 四、避坑提醒

1. **不要全量存储完整 Prompt 与返回值**：建议截断前 200 字符或只存关键字段，否则文件体积会指数级膨胀
2. **必须做日志轮转**：按文件大小（如单文件 100MB）或按天切割，定期清理 7 天以上的历史追踪文件
3. **Trace 上下文必须贯穿全程**：LangGraph 节点间、MCP 异步桥接中必须透传`trace_id`，不然链路断裂，文件里全是零散无关联的 Span
4. **异常场景不能丢 Span**：用`try/finally`确保 Span 正常写入，报错时也要记录错误栈，不能只记录成功调用

## 五、平滑演进路线（无需推翻重写）

1. **阶段 1（当前）**：本地 JSON Lines 存储，优先把全链路埋点打全、上下文传递逻辑跑通
2. **阶段 2（可视化需求）**：写简单 Python 脚本读取 JSONL 生成 HTML 时序表，或用`jq`命令行做统计查询
