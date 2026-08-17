"""
桌面宠物后台服务

提供：
- 后台状态追踪：订阅 EventBus，追踪 agent 运行状态
- 系统通知：任务完成/出错时弹出桌面通知
- 全局热键：注册全局快捷键唤起 TUI
- 状态持久化：将状态写入文件供 TUI 读取
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from mokioclaw.core.events import EventBus, get_event_bus
from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


class AgentStatus(Enum):
    """Agent 运行状态"""
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"
    COMPLETED = "completed"


class TaskStatus(Enum):
    """任务执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StatusSnapshot:
    """状态快照"""
    agent_status: str = "idle"
    current_task: str = ""
    task_status: str = "idle"
    last_action: str = ""
    last_action_time: str = ""
    error_count: int = 0
    completed_tasks: int = 0
    total_tasks: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_status": self.agent_status,
            "current_task": self.current_task,
            "task_status": self.task_status,
            "last_action": self.last_action,
            "last_action_time": self.last_action_time,
            "error_count": self.error_count,
            "completed_tasks": self.completed_tasks,
            "total_tasks": self.total_tasks,
            "uptime_seconds": self.uptime_seconds,
        }


class DesktopPetAgent:
    """桌面宠物后台服务

    功能：
    1. 订阅 EventBus 追踪 agent 状态变化
    2. 任务完成/出错时触发系统通知
    3. 将状态持久化到文件供 TUI 读取
    4. 注册全局热键回调

    使用方式：
        pet = DesktopPetAgent(workspace=Path("."))
        pet.start()
        # ... agent runs ...
        pet.stop()
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace
        self._status = StatusSnapshot()
        self._lock = threading.RLock()
        self._unsub: Callable[[], None] | None = None
        self._running = False
        self._start_time = datetime.now(timezone.utc)
        self._status_file: Path | None = (
            workspace / ".mokioclaw" / "pet-status.json" if workspace is not None else None
        )
        self._on_hotkey: Callable[[], None] | None = None
        self._hotkey_listener: Any = None
        # writer 线程的 per-generation 停止事件（stop→start 快速切换防线程泄漏）
        self._writer_stop: threading.Event | None = None

    @property
    def status(self) -> StatusSnapshot:
        with self._lock:
            return self._status

    def start(self) -> None:
        """启动后台服务"""
        if self._running:
            return
        self._running = True
        self._start_time = datetime.now(timezone.utc)

        # 订阅 EventBus
        bus = get_event_bus()
        self._unsub = bus.subscribe_all(self._on_event, priority=5)

        # 启动状态持久化线程
        self._start_status_writer()

        logger.info("Desktop pet agent started")

    def stop(self) -> None:
        """停止后台服务"""
        self._running = False
        # 唤醒 writer 线程使其退出（用 per-generation event，
        # 避免 stop→start 快速切换时旧线程看到新 _running=True 而永不退出）
        if self._writer_stop is not None:
            self._writer_stop.set()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._stop_hotkey_listener()
        logger.info("Desktop pet agent stopped")

    def set_hotkey(self, callback: Callable[[], None]) -> None:
        """设置全局热键回调

        Args:
            callback: 热键触发时的回调函数
        """
        self._on_hotkey = callback
        if self._running:
            self._start_hotkey_listener()

    def notify(self, title: str, message: str, level: str = "info") -> None:
        """发送系统通知

        Args:
            title: 通知标题
            message: 通知内容
            level: 级别 — "info" / "success" / "warning" / "error"
        """
        _send_notification(title, message, level)

    def get_status_file(self) -> Path | None:
        """获取状态文件路径（TUI 通过此路径读取状态）"""
        if self._status_file is None and self._workspace is not None:
            self._status_file = self._workspace / ".mokioclaw" / "pet-status.json"
        return self._status_file

    def _on_event(self, event: dict[str, Any]) -> None:
        """处理 EventBus 事件

        通知在锁外发送（notify 起子进程最长阻塞 5s，锁内执行会卡住
        事件总线的其他订阅者和状态写入线程）。
        """
        event_type = event.get("type", "")
        pending_notify: tuple[str, str, str] | None = None

        with self._lock:
            if event_type == "intent_decision":
                route = event.get("route", "workflow")
                if route == "chat":
                    self._status.agent_status = AgentStatus.IDLE.value
                else:
                    self._status.agent_status = AgentStatus.THINKING.value
                self._status.last_action = f"Intent routed: {route}"
                self._status.last_action_time = _now_iso()

            elif event_type == "session_turn_started":
                # 记录当前任务描述（完成通知 / widget 任务行展示用）
                task_text = str(event.get("task", "") or "").strip()
                if task_text:
                    self._status.current_task = task_text[:200]
                self._status.agent_status = AgentStatus.THINKING.value
                self._status.last_action = "Turn started"
                self._status.last_action_time = _now_iso()

            elif event_type == "plan_snapshot":
                node = event.get("node", "")
                self._status.agent_status = AgentStatus.THINKING.value
                self._status.last_action = f"Plan: {node}"
                self._status.last_action_time = _now_iso()

            elif event_type == "tool_call":
                node = event.get("node", "")
                name = event.get("name", "")
                self._status.agent_status = AgentStatus.TOOL_CALL.value
                self._status.last_action = f"{node}.{name}"
                self._status.last_action_time = _now_iso()

            elif event_type == "tool_result":
                node = event.get("node", "")
                result = event.get("result", {})
                ok = result.get("ok") if isinstance(result, dict) else None
                if ok is False:
                    self._status.agent_status = AgentStatus.ERROR.value
                    self._status.error_count += 1
                    self._status.last_action = f"{node}: FAILED"
                    self._status.last_action_time = _now_iso()
                    pending_notify = (
                        f"dot_agent: {node} 工具执行失败",
                        str(result.get("error", "unknown error"))[:200],
                        "error",
                    )
                else:
                    self._status.agent_status = AgentStatus.THINKING.value
                    self._status.last_action = f"{node}: OK"
                    self._status.last_action_time = _now_iso()

            elif event_type == "handoff":
                from_a = event.get("from", "?")
                to_a = event.get("to", "?")
                self._status.last_action = f"Handoff: {from_a} → {to_a}"
                self._status.last_action_time = _now_iso()

            elif event_type == "final":
                self._status.agent_status = AgentStatus.COMPLETED.value
                self._status.task_status = TaskStatus.DONE.value
                self._status.completed_tasks += 1
                self._status.last_action = "Task completed"
                self._status.last_action_time = _now_iso()
                pending_notify = (
                    "dot_agent: 任务完成",
                    self._status.current_task[:100] or "Task finished",
                    "success",
                )

            elif event_type == "checkpoint_saved":
                self._status.last_action = f"Checkpoint saved: {event.get('path', '?')}"
                self._status.last_action_time = _now_iso()

            elif event_type == "context_compression":
                before = event.get("before_tokens", 0)
                after = event.get("after_tokens", 0)
                self._status.last_action = f"Compressed: {before} → {after} tokens"
                self._status.last_action_time = _now_iso()

            elif event_type == "chat_response":
                self._status.agent_status = AgentStatus.IDLE.value
                self._status.last_action = "Chat reply"
                self._status.last_action_time = _now_iso()

        if pending_notify is not None:
            self.notify(pending_notify[0], pending_notify[1], level=pending_notify[2])

    def _start_status_writer(self) -> None:
        """启动状态文件写入线程（每次 start 一个新 stop event，旧线程可靠退出）"""
        self._writer_stop = threading.Event()
        stop_event = self._writer_stop

        def _writer() -> None:
            while self._running and not stop_event.is_set():
                try:
                    status_file = self.get_status_file()
                    if status_file is not None:
                        status_file.parent.mkdir(parents=True, exist_ok=True)
                        with self._lock:
                            snapshot = self._status.to_dict()
                        snapshot["updated_at"] = _now_iso()
                        status_file.write_text(
                            json.dumps(snapshot, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                except Exception as exc:
                    logger.debug("status writer error: %s", exc)
                stop_event.wait(1)

        t = threading.Thread(target=_writer, daemon=True, name="pet-status-writer")
        t.start()

    def _start_hotkey_listener(self) -> None:
        """启动全局热键监听（重复设置时先停旧监听器，避免热键重复注册）"""
        if self._on_hotkey is None:
            return
        self._stop_hotkey_listener()
        self._hotkey_listener = _register_hotkey(self._on_hotkey)

    def _stop_hotkey_listener(self) -> None:
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None


# ============================================================
# 系统通知（跨平台）
# ============================================================

def _send_notification(title: str, message: str, level: str = "info") -> None:
    """发送桌面通知"""
    try:
        _notify_platform(title, message, level)
    except Exception as exc:
        logger.debug("notification failed: %s", exc)


def _notify_platform(title: str, message: str, level: str) -> None:
    """平台特定通知实现"""
    import platform
    system = platform.system()

    if system == "Windows":
        _notify_windows(title, message)
    elif system == "Darwin":
        _notify_macos(title, message)
    elif system == "Linux":
        _notify_linux(title, message)
    else:
        # 兜底：输出到日志
        logger.info("[Notification] %s: %s", title, message)


def _notify_windows(title: str, message: str) -> None:
    """Windows 桌面通知（使用 Base64 编码的 PowerShell 命令避免注入）"""
    try:
        import base64
        import subprocess

        # 构建 Toast XML（title/message 需 XML 转义）
        from xml.sax.saxutils import escape as _xml_escape

        toast_xml = (
            '<toast><visual><binding template="ToastText02">'
            f'<text id="1">{_xml_escape(title)}</text>'
            f'<text id="2">{_xml_escape(message[:200])}</text>'
            '</binding></visual></toast>'
        )

        # 构建 PowerShell 脚本（使用 here-string 避免引号转义问题）
        ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.Data.Xml.Dom.XmlDocument]::new()
$template.LoadXml(@"
{toast_xml}
"@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("dot_agent").Show($toast)
'''
        # 使用 Base64 编码的 -EncodedCommand 完全避免引号注入
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=5,
            creationflags=creation_flags,
        )
    except Exception as exc:
        logger.debug("Windows toast notification failed: %s", exc)
        # 回退：win10toast
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, message[:200], duration=3, threaded=True)
        except ImportError:
            pass
        except Exception as fallback_exc:
            logger.debug("win10toast fallback also failed: %s", fallback_exc)


def _applescript_escape(value: str) -> str:
    """AppleScript 字符串字面量转义（反斜杠 + 双引号）"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, message: str) -> None:
    """macOS 桌面通知

    title/message 来自工具错误文本等不受信内容，必须转义后才能拼进
    AppleScript 字符串——一个裸引号即可逃逸执行任意 osascript。
    """
    import subprocess

    script = (
        f'display notification "{_applescript_escape(message)}" '
        f'with title "{_applescript_escape(title)}" sound name "default"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)


def _notify_linux(title: str, message: str) -> None:
    """Linux 桌面通知（notify-send）"""
    import subprocess
    subprocess.run(
        ["notify-send", title, message[:200]],
        capture_output=True,
        timeout=5,
    )


# ============================================================
# 全局热键
# ============================================================

def _register_hotkey(callback: Callable[[], None]) -> Any:
    """注册全局热键（平台相关）

    Windows/Linux: 尝试 pynput，失败则回退到输入监听
    macOS: 使用 Quartz 事件 tap
    """
    try:
        from pynput import keyboard

        def _on_press(key):
            try:
                if key == keyboard.Key.f8 or (hasattr(key, 'char') and key.char == '`'):
                    callback()
            except AttributeError:
                pass

        listener = keyboard.GlobalHotKeys({"<f8>": callback, "<alt>+`": callback})
        listener.start()
        return listener
    except ImportError:
        logger.debug("pynput not available, hotkey disabled")
        return None
    except Exception as exc:
        logger.debug("hotkey registration failed: %s", exc)
        return None


# ============================================================
# 辅助函数
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
