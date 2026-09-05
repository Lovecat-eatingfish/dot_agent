"""
dot.coding.trace.controller — TraceController（链路追踪生命周期）

负责 TraceCollector 的挂载 / 卸载 / 运行时开关 / 状态查询 / 兜底落盘。
从 CodingHost 拆出，host 只做委托；会话切换通过 session_provider 动态取值。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import make_trace_collector, trace_enabled

if TYPE_CHECKING:
    from dot.agent.harness import AgentHarness

    from ..session import Session

logger = logging.getLogger(__name__)


class TraceController:
    """管理当前 harness 的链路追踪订阅（DOT_TRACE_ENABLED=0 时为 Noop）"""

    def __init__(
            self,
            workspace: Path,
            session_provider: Callable[[], "Session"],
    ) -> None:
        self._workspace = workspace
        self._session_provider = session_provider
        self._unsub: Callable[[], None] | None = None
        self._collector: Any = None

    def attach(self, harness: "AgentHarness | None") -> None:
        """为当前 harness 订阅 TraceCollector（重挂前先卸载旧订阅）"""
        self.detach()
        session = self._session_provider()
        self._collector = make_trace_collector(self._workspace, session.session_id)
        if harness is not None:
            self._unsub = harness.subscribe(self._collector.on_event)

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def set_enabled(self, enabled: bool, *, harness: "AgentHarness | None" = None) -> None:
        """运行时开关链路追踪（重挂 collector）"""
        os.environ["DOT_TRACE_ENABLED"] = "1" if enabled else "0"
        self.attach(harness)

    def info(self) -> dict:
        """追踪状态与落盘目录"""
        return {
            "enabled": trace_enabled(),
            "session_id": self._session_provider().session_id,
            "output_dir": self._workspace / ".dot" / "traces",
        }

    def flush(self) -> None:
        """进程退出前兜底落盘未结束的 span"""
        if self._collector is not None:
            self._collector.flush()
