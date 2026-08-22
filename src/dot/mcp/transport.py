"""
MCP 传输层

对齐 Claude Code 多传输支持：stdio + http/SSE。

- MCPTransportBase: 传输抽象基类（connect/send/receive/disconnect/is_connected）
- MCPTransport (StdioTransport): 通过 subprocess 与外部 MCP Server 进程通信（向后兼容别名）
- HttpSSETransport: 通过 HTTP POST 发请求 + SSE 流读响应（streamable-http / SSE 传输）

设计：MCPClient 只依赖抽象基类的 5 个方法形状，与具体传输无关。
"""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from queue import Queue
from typing import Any


class MCPTransportBase:
    """MCP 传输抽象基类

    所有传输实现必须提供：connect / disconnect / send / receive / is_connected。
    MCPClient 仅依赖此 5 个方法。
    """

    def connect(self, timeout: float = 10.0) -> None:  # noqa: ARG002
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def receive(self, timeout: float = 30.0) -> dict[str, Any] | None:  # noqa: ARG002
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError


class MCPTransport(MCPTransportBase):
    """stdio 传输：与子进程通过 stdin/stdout 通信

    保留原类名作为向后兼容别名（bridge.register_server 默认使用此类）。
    """

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

    def connect(self, timeout: float = 10.0) -> None:  # noqa: ARG002
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
            # MCP 规范要求 stdio 走 UTF-8；不指定时 Windows 按 locale（GBK）编解码，
            # Node 系 server 的 UTF-8 输出会让读线程 UnicodeDecodeError 静默死亡
            encoding="utf-8",
            errors="replace",
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


# 向后兼容别名
StdioTransport = MCPTransport


class HttpSSETransport(MCPTransportBase):
    """HTTP/SSE 传输：POST JSON-RPC 请求，从 SSE 流读响应

    对齐 Claude Code streamable-http / SSE 传输。适用于远程 MCP Server。

    协议假设：
    - send: POST JSON-RPC 消息到 self._url，Content-Type: application/json
    - 服务端可直接在 HTTP 响应体返回 JSON-RPC 响应（非 SSE），
      或通过 SSE 长连接（Content-Type: text/event-stream）推送。
    - receive: 先消费最近一次 POST 的同步响应，再从 SSE 队列取。

    使用标准库 urllib，不引入 httpx/requests 依赖。
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if headers:
            self._headers.update(headers)
        self._timeout = timeout
        self._connected = False
        self._response_queue: Queue[dict[str, Any]] = Queue()
        # SSE 流读取线程（仅当服务端返回 text/event-stream 时启动）
        self._sse_thread: threading.Thread | None = None
        self._sse_response = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0) -> None:  # noqa: ARG002
        """连接校验：对 HTTP 传输而言，connect 即视为就绪。

        不在此发起实际请求（按需在 send 时发 POST）。
        """
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
        # 关闭 SSE 流
        self._close_sse_stream()

    def _close_sse_stream(self) -> None:
        """关闭当前 SSE 流并等待读取线程退出（带超时）"""
        with self._lock:
            resp = self._sse_response
            thread = self._sse_thread
            self._sse_response = None
            self._sse_thread = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def send(self, message: dict[str, Any]) -> None:
        """POST JSON-RPC 消息；同步响应入队供 receive 取"""
        if not self._connected:
            raise RuntimeError("HttpSSETransport not connected")
        raw = json.dumps(message, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=raw,
            headers=self._headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"MCP HTTP error {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MCP HTTP connection failed: {exc}") from exc

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in content_type:
            # SSE 流：后台线程持续解析 event 推入队列。
            # 启动新 SSE 前先关闭旧流，避免资源/线程泄漏；
            # 关闭旧流 + 赋值新流整个在锁内完成，消除并发 send 的赋值竞态。
            self._start_sse_stream(resp)
        else:
            # 同步 JSON 响应：直接入队
            try:
                body = resp.read().decode("utf-8", errors="replace").strip()
            finally:
                resp.close()
            if body:
                self._enqueue_raw(body)

    def receive(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """从响应队列读取一条 JSON-RPC 消息"""
        try:
            return self._response_queue.get(timeout=timeout)
        except Exception:
            return None

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # ===== 内部 =====

    def _enqueue_raw(self, raw: str) -> None:
        """解析一行 JSON 入队；非 JSON 忽略"""
        try:
            self._response_queue.put(json.loads(raw))
        except Exception:
            pass

    def _start_sse_stream(self, resp: Any) -> None:
        """关闭旧 SSE 流并在锁内原子地绑定新流 + 启动读取线程。

        将 _close_sse_stream 的清空 + 赋值 _sse_response/_sse_thread + start
        收敛到单一临界区，消除并发 send 时 _sse_response 赋值竞态（M2）。
        """
        old_resp = None
        old_thread = None
        with self._lock:
            old_resp = self._sse_response
            old_thread = self._sse_thread
            self._sse_response = resp
            self._sse_thread = threading.Thread(
                target=self._read_sse_stream, args=(resp,), daemon=True
            )
            new_thread = self._sse_thread
        # 锁外关闭旧流（resp.close 可能阻塞，避免持锁）
        if old_resp is not None:
            try:
                old_resp.close()
            except Exception:
                pass
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
        new_thread.start()

    def _read_sse_stream(self, resp: Any) -> None:
        """后台线程：解析 SSE 流，按空行聚合事件后入队

        SSE 规范：一个事件可含多行 data:，以 \n 拼接，空行分隔事件。
        逐行读 data: 拼到缓冲，遇空行就把整块 payload 入队。

        流结束（EOF 或异常）后关闭 resp 并清空 _sse_response（M3）：
        原实现 finally 仅置 _connected=False 但不关 resp、不置 None，
        导致旧 resp 泄漏且 send() 因 _connected=False 永久 raise、
        走不到 _close_sse_stream —— 传输在 SSE EOF 后永久不可用。
        """
        buffer: list[str] = []
        try:
            for raw_line in resp:
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.rstrip("\r\n")
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload:
                        buffer.append(payload)
                elif line == "":
                    # 空行 = 事件结束
                    if buffer:
                        self._enqueue_raw("\n".join(buffer))
                        buffer = []
                # 其他行（event:/id:/comment）忽略
            # 流结束时若还有未 flush 的缓冲
            if buffer:
                self._enqueue_raw("\n".join(buffer))
        except Exception:
            pass
        finally:
            # 关闭 resp + 清空引用（锁内，与 _start_sse_stream 协调）
            with self._lock:
                # 仅当仍指向本线程绑定的 resp 时才清空，避免误清新流
                if self._sse_response is resp:
                    self._sse_response = None
                    self._sse_thread = None
            try:
                resp.close()
            except Exception:
                pass
