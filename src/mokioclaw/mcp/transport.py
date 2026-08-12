"""
MCP 传输层

实现 stdio 传输：通过 subprocess 与外部 MCP Server 进程通信。
"""
from __future__ import annotations

import json
import subprocess
import threading
from queue import Queue
from typing import Any, Callable


class MCPTransport:
    """stdio 传输：与子进程通过 stdin/stdout 通信"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._stdout_queue: Queue[str] = Queue()
        self._stderr_queue: Queue[str] = Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._connected = False
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0) -> None:
        """启动子进程并开始读取 stdout"""
        import os
        env = {**os.environ}
        if self._env:
            env.update(self._env)

        self._process = subprocess.Popen(
            [self._command, *self._args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            text=True,
            bufsize=1,  # 行缓冲
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        """断开连接并终止子进程"""
        with self._lock:
            self._connected = False
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception:
                pass
            self._process = None
        # 等待读者线程结束
        for thread_name in ("_reader_thread", "_stderr_thread"):
            thread = getattr(self, thread_name, None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
                setattr(self, thread_name, None)

    def send(self, message: dict[str, Any]) -> None:
        """发送 JSON-RPC 消息到子进程 stdin"""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Transport not connected")
        raw = json.dumps(message, ensure_ascii=False, default=str)
        try:
            self._process.stdin.write(raw + "\n")
            self._process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise RuntimeError(f"Failed to send message to MCP server: {exc}") from exc

    def receive(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """从 stdout 队列读取一条 JSON-RPC 消息"""
        try:
            raw = self._stdout_queue.get(timeout=timeout)
            return json.loads(raw)
        except Exception:
            return None

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and self._process is not None and self._process.poll() is None

    def _read_stdout(self) -> None:
        """后台线程：持续读取 stdout 并放入队列"""
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if line:
                self._stdout_queue.put(line)
        self._connected = False

    def _read_stderr(self) -> None:
        """后台线程：持续读取 stderr（用于日志）"""
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            line = line.strip()
            if line:
                self._stderr_queue.put(line)

    def get_stderr(self) -> list[str]:
        """获取 stderr 日志"""
        lines = []
        while not self._stderr_queue.empty():
            lines.append(self._stderr_queue.get_nowait())
        return lines

    def get_pid(self) -> int | None:
        if self._process is None:
            return None
        return self._process.pid
