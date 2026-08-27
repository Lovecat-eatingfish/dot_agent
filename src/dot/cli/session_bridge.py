"""
会话桥接层 — CLI ↔ Agent Graph 适配器（对齐设计文档 §2 第 3 层）

职责：
  - 包装 AgentHost，对 CLI/TUI 提供统一调用面
  - 驱动一轮图执行，把 graph stream chunk + session.messages diff 归一成
    DisplayEvent 流，供 TUI 渲染（CLI 只订阅/展示，不实现业务）
  - 管理运行模式（run_mode）、输入历史、Ctrl+C 取消
  - 承接斜杠命令需要的会话/MCP/压缩/工作目录操作（全部委托核心，不重写）

设计约束：本层不碰 graph 节点、压缩算法、Hook；只调度 + 事件归一 + 展示数据读取。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, TypedDict

from ..core.log import get_logger
from ..host.agent_host import AgentHost
from .modes import normalize_mode

logger = get_logger(__name__)


# ============================================================
# 展示事件（TUI 据此渲染）
# ============================================================

class DisplayEvent(TypedDict, total=False):
    """归一化后的展示事件（TUI 消费）"""
    kind: str  # user | assistant | tool_call | tool_result | plan | validation
    #         | node | final | intervention | cancelled | error | done | system
    text: str
    name: str
    args: Any
    content: str
    plan: Any
    passed: bool
    summary: str  # final 事件：本次任务总结
    answer: str   # final 事件：最终答复文本
    reason: str
    node: str


# ============================================================
# SessionBridge
# ============================================================

class SessionBridge:
    """CLI ↔ Agent Graph 桥接器

    使用方式：
        bridge = SessionBridge(host)
        bridge.set_mode("code")
        for ev in bridge.run_turn("写个快排"):
            tui.render(ev)
    """

    def __init__(self, host: AgentHost, config: "CLIConfig | None" = None) -> None:
        self.host = host
        self.config = config  # CLIConfig 实例，供 slash 命令使用
        self._run_mode: str = "agent"
        # 输入历史（↑/↓ 回溯）
        self._history: list[str] = []
        self._history_pos: int = -1  # -1 表示不在历史中游走
        # 把当前会话的 run_mode 同步到 bridge（恢复历史会话时）
        try:
            self._run_mode = self.host.get_run_mode()
        except Exception:
            self._run_mode = "agent"

    # ============================================================
    # 运行模式
    # ============================================================

    def get_mode(self) -> str:
        return self._run_mode

    def set_mode(self, mode: str) -> None:
        self._run_mode = normalize_mode(mode)
        # 写入 session.run_mode，下一轮热生效
        self.host.set_run_mode(self._run_mode)

    def cycle_mode(self, *, forward: bool = True) -> str:
        from .modes import cycle_mode
        new = cycle_mode(self._run_mode, forward=forward)
        self.set_mode(new)
        return new

    # ============================================================
    # 输入历史
    # ============================================================

    def add_history(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # 去重相邻重复
        if self._history and self._history[-1] == text:
            return
        self._history.append(text)
        self._history_pos = -1

    def history_prev(self) -> str | None:
        if not self._history:
            return None
        if self._history_pos == -1:
            self._history_pos = len(self._history) - 1
        elif self._history_pos > 0:
            self._history_pos -= 1
        return self._history[self._history_pos]

    def history_next(self) -> str | None:
        if not self._history or self._history_pos < 0:
            return None
        if self._history_pos < len(self._history) - 1:
            self._history_pos += 1
            return self._history[self._history_pos]
        # 到达末尾：退出历史游走
        self._history_pos = -1
        return ""

    def reset_history_cursor(self) -> None:
        self._history_pos = -1

    # ============================================================
    # 工作目录
    # ============================================================

    def get_workspace(self) -> str:
        session = self.host.session
        if session is not None:
            cwd = getattr(session, "cwd", None)
            if cwd:
                return str(cwd)
            return str(session.workspace)
        return str(self.host.workspace)

    def switch_workspace(self, path: str) -> None:
        """切换工作目录：影响 BashTool 执行目录（会话级 cwd）

        注意：AgentHost 的 workspace（路径安全/会话根）在初始化时固定，
        此处切换的是 agent 执行命令的工作目录，不是 workspace 隔离边界。
        """
        target = Path(path).expanduser()
        session = self.host.get_or_create_session()
        if not target.exists():
            raise FileNotFoundError(f"目录不存在: {target}")
        session.cwd = target
        logger.info("[bridge] workspace cwd switched: %s", target)

    # ============================================================
    # 会话生命周期
    # ============================================================

    def reset_session(self) -> str:
        """重建全新 Session（/reset）"""
        session = self.host.new_session()
        self.host.set_run_mode(self._run_mode)
        return f"新会话: {session.session_id} (turn {session.current_turn_id})"

    def save_session(self, name: str | None = None) -> str:
        return self.host.save_current_session(name)

    def load_session(self, name: str) -> str:
        session = self.host.load_session_by_name(name)
        # 恢复 run_mode 到 bridge
        self._run_mode = getattr(session, "run_mode", "agent")
        return f"{session.session_id} (turn {session.current_turn_id}, {len(session.messages)} msgs, mode={self._run_mode})"

    # ============================================================
    # MCP
    # ============================================================

    def mcp_list(self) -> str:
        status = self.host.get_mcp_status()
        servers = status.get("servers", [])
        tools = status.get("tools", [])
        if not tools:
            return "[mcp] 无可用 MCP 工具（未配置 .dot/mcp.json 或连接失败）"
        lines = [f"[mcp] 在线: {status.get('online')}  servers={len(servers)}  tools={len(tools)}"]
        if servers:
            lines.append("  servers: " + ", ".join(servers))
        for t in tools[:60]:
            lines.append(f"  - {t}")
        if len(tools) > 60:
            lines.append(f"  ... 还有 {len(tools) - 60} 个工具")
        return "\n".join(lines)

    def mcp_restart(self) -> str:
        status = self.host.restart_mcp()
        tools = status.get("tools", [])
        return f"重启完成: servers={len(status.get('servers', []))} tools={len(tools)}"

    # ============================================================
    # 上下文压缩（只读展示 + 调试触发，不重写算法）
    # ============================================================

    def compact_status(self) -> str:
        st = self.host.get_token_status()
        if not st:
            return "[compact] 无会话"
        return (
            "[compact/status]\n"
            f"  Token 水位: {st.get('water_level', 0)}% "
            f"({st.get('current_tokens', 0)}/{st.get('context_window', 0)})\n"
            f"  消息数量: {st.get('message_count', 0)}\n"
            f"  压缩阈值: L1={st.get('l1_threshold', 0)} "
            f"L2={st.get('l2_threshold', 0)} L3={st.get('l3_threshold', 0)}\n"
            f"  累计压缩: {st.get('total_compressions', 0)} 次, "
            f"节省 {st.get('total_tokens_saved', 0)} tokens"
        )

    def force_compact(self) -> str:
        """/compact force：触发一次核心压缩节点（复用 context_compress_node，不重写）

        仅当 token 超过阈值时真正压缩（由核心 planner 决策），未超阈值则跳过。
        """
        session = self.host.get_or_create_session()
        if session is None:
            return "无活跃会话"
        try:
            from ..compress.node import context_compress_node

            before_msgs = len(session.messages)
            before_tokens = self.host.get_token_status().get("current_tokens", 0)
            context_compress_node({"session": session})
            after_msgs = len(session.messages)
            after_tokens = self.host.get_token_status().get("current_tokens", 0)
            if before_msgs == after_msgs:
                return f"未达压缩阈值，跳过（{before_msgs} msgs, {before_tokens} tokens）"
            return f"压缩完成: {before_msgs}→{after_msgs} msgs, {before_tokens}→{after_tokens} tokens"
        except Exception as exc:
            logger.warning("[bridge] force_compact failed: %s", exc, exc_info=True)
            return f"压缩触发失败: {exc}"

    # ============================================================
    # 执行一轮（核心：事件归一）
    # ============================================================

    def is_running(self) -> bool:
        session = self.host.session
        return bool(session and getattr(session, "is_running", False))

    def cancel(self) -> bool:
        """Ctrl+C：中断当前 turn，不退出终端"""
        return self.host.request_cancel()

    def has_pending_intervention(self) -> bool:
        return self.host.has_pending_intervention()

    def resume_intervention(self, action: str) -> Iterator[DisplayEvent]:
        """人工介入后恢复（continue 重新规划 / stop 结束）"""
        session = self.host.get_or_create_session()
        seen = len(session.messages) if session else 0
        self.host.set_run_mode(self._run_mode)
        try:
            for chunk in self.host.resume_intervention(action, agent_mode="auto"):
                events, seen = self._diff_messages(session, seen)
                for ev in events:
                    yield ev
                ev = self._handle_chunk(chunk)
                if ev is not None:
                    yield ev
            events, seen = self._diff_messages(session, seen)
            for ev in events:
                yield ev
        except Exception as exc:
            yield self._error(f"恢复失败: {exc}")
            return
        if self.has_pending_intervention():
            yield self._ev("intervention", reason="replan_or_attempt_exhausted")
        yield self._ev("done")

    def run_turn(self, user_input: str) -> Iterator[DisplayEvent]:
        """执行一轮：驱动 graph，归一事件流供 TUI 渲染

        流程：
        1. 写入 run_mode 到 session（热生效）
        2. 记录 messages 基线 seen
        3. 迭代 host.run(...)：每收到一个 chunk，先 flush 新增消息（user/ai/tool）
           再处理节点级事件（plan/valid/final/intervention/cancelled）
        4. 异常 → error 事件（不崩溃 TUI，可继续对话）
        """
        self.add_history(user_input)
        session = self.host.get_or_create_session()
        self.host.set_run_mode(self._run_mode)
        seen = len(session.messages) if session else 0

        # 人工介入遗留：直接走恢复流
        if self.has_pending_intervention():
            yield from self.resume_intervention("continue")
            return

        try:
            for chunk in self.host.run(user_input, agent_mode="auto"):
                events, seen = self._diff_messages(session, seen)
                for ev in events:
                    yield ev
                ev = self._handle_chunk(chunk)
                if ev is not None:
                    yield ev
            events, seen = self._diff_messages(session, seen)
            for ev in events:
                yield ev
        except Exception as exc:
            logger.error("[bridge] run_turn failed: %s", exc, exc_info=True)
            yield self._error(f"执行出错: {exc}")
            return

        if self.has_pending_intervention():
            yield self._ev("intervention", reason="replan_or_attempt_exhausted")
        yield self._ev("done")

    # ============================================================
    # 事件归一（消息 diff + chunk 解析）
    # ============================================================

    # run_turn 中实际使用的 flush：返回 (events, new_seen)
    def _diff_messages(self, session: Any, seen: int) -> tuple[list[DisplayEvent], int]:
        """计算 session.messages[seen:] 对应的展示事件列表 + 新 seen"""
        events: list[DisplayEvent] = []
        if session is None:
            return events, seen
        msgs = list(session.messages)  # 快照，避免迭代中被修改
        i = seen
        while i < len(msgs):
            events.extend(self._message_to_events(msgs[i]))
            i += 1
        return events, len(msgs)

    def _message_to_events(self, msg: Any) -> list[DisplayEvent]:
        """单条 LangChain 消息 → 展示事件列表"""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        if isinstance(msg, SystemMessage):
            return []  # 系统提示词不展示

        if isinstance(msg, HumanMessage):
            content = _msg_text(msg)
            return [self._ev("user", text=content)] if content else []

        if isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or "tool"
            content = _msg_text(msg)
            return [self._ev("tool_result", name=name, content=_truncate(content, 2000))]

        if isinstance(msg, AIMessage):
            events: list[DisplayEvent] = []
            tool_calls = getattr(msg, "tool_calls", None) or []
            for call in tool_calls:
                events.append(self._ev(
                    "tool_call",
                    name=str(call.get("name", "")),
                    args=call.get("args", {}),
                ))
            content = _msg_text(msg)
            if content:
                events.append(self._ev("assistant", text=content))
            return events

        # 其它类型（如占位）兜底
        content = _msg_text(msg)
        return [self._ev("system", text=content)] if content else []

    def _handle_chunk(self, chunk: dict[str, Any]) -> DisplayEvent | None:
        """处理 graph stream chunk（节点级事件）"""
        if not isinstance(chunk, dict):
            return None
        # 取消标记
        if "__dot_cancelled__" in chunk:
            return self._ev("cancelled")

        # 单节点更新：{node_name: update_dict}
        for node_name, update in chunk.items():
            if not isinstance(update, dict):
                continue
            if node_name == "finally_node":
                answer = str(update.get("final_answer", ""))
                summary = str(update.get("summary", ""))
                return self._ev("final", answer=answer, text=summary)
            if node_name == "human_intervene":
                return self._ev("intervention", reason="replan_or_attempt_exhausted")
            # 其它节点：作为进度提示（去重由 TUI 处理）
            return self._ev("node", node=node_name)
        return None

    # ============================================================
    # 事件构造小工具
    # ============================================================

    @staticmethod
    def _ev(kind: str, **fields: Any) -> DisplayEvent:
        return {"kind": kind, **fields}  # type: ignore[return-value]

    @staticmethod
    def _error(text: str) -> DisplayEvent:
        return {"kind": "error", "text": text}  # type: ignore[return-value]


# ============================================================
# Helpers
# ============================================================

def _msg_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 多模态：拼文本片段
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content) if content else ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
