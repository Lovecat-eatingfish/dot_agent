"""
dot.coding.session.state — SessionState / ToolContext 职责分离

Session 拆分为两部分（来自 Tau 的洞察）：

- **SessionState**: 可序列化的会话状态（session_id / workspace / messages / config）
  能安全地持久化到 JSONL，不持有任何不可序列化的运行时对象。

- **ToolContext**: 不可序列化的运行时上下文（cwd / hook_runner / mcp_host / read_files / message_seq）
  工具通过 ToolContext 访问运行时依赖，不直接访问 Session。
  消除 runtime_registry 全局注册表的必要性。

这样拆分后，工具执行通过 ToolContext 访问依赖，持久化层只调用
to_snapshot() / from_snapshot()，不直接读写字段。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dot.ai.types import AgentMessage


# ============================================================
# SessionState — 可序列化的会话状态
# ============================================================

@dataclass
class SessionState:
    """可序列化的会话状态

    能安全持久化到 JSONL，不持有不可序列化的运行时对象。
    """
    session_id: str
    workspace: Path = field(default_factory=Path.cwd)
    messages: list[AgentMessage] = field(default_factory=list)
    agent_mode: str = "auto"
    max_turns: int | None = None
    max_replan: int = 3
    title: str = ""

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为快照字典（用于 JSONL 持久化）"""
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "message_count": len(self.messages),
            "agent_mode": self.agent_mode,
            "max_turns": self.max_turns,
            "max_replan": self.max_replan,
            "title": self.title,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> SessionState:
        """从快照字典恢复"""
        return cls(
            session_id=data.get("session_id", ""),
            workspace=Path(data.get("workspace", ".")),
            messages=[],  # 消息从 JSONL 记录流恢复，不在此处
            agent_mode=data.get("agent_mode", "auto"),
            max_turns=data.get("max_turns"),
            max_replan=data.get("max_replan", 3),
            title=data.get("title", ""),
        )


# ============================================================
# ToolContext — 不可序列化的运行时上下文
# ============================================================

class HookRunner(Protocol):
    """Hook 运行器协议（不可序列化）"""
    def run_tool_call_hooks(self, call: Any) -> Any: ...
    def run_tool_result_hooks(self, call: Any, result: Any, is_error: bool) -> Any: ...


class McpHost(Protocol):
    """MCP 主机协议（不可序列化）"""
    def get_tools(self) -> list[Any]: ...
    def call_tool(self, name: str, args: dict) -> Any: ...


@dataclass
class ToolContext:
    """不可序列化的运行时上下文

    工具通过 ToolContext 访问运行时依赖，不直接访问 Session。
    消除 runtime_registry 全局注册表的必要性。

    字段说明：
      - cwd: 当前工作目录
      - hook_runner: hook 链运行器（ExtensionRuntime）
      - mcp_host: MCP 主机（内置扩展）
      - read_files: 文件读取快照（写入保护）
      - message_seq: 消息序号生成器（线程安全）
    """
    cwd: Path = field(default_factory=Path.cwd)
    hook_runner: HookRunner | None = None
    mcp_host: McpHost | None = None
    read_files: dict[Path, Any] = field(default_factory=dict)
    _message_seq: int = 0
    _message_seq_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def next_message_id(self) -> str:
        """生成下一个消息 ID（线程安全）"""
        with self._message_seq_lock:
            self._message_seq += 1
            return f"msg-{self._message_seq:05d}"
