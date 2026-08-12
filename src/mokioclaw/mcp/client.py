"""
MCP Client

连接单个 MCP Server，处理 initialize 握手、工具发现和工具调用。
每个 MCPClient 实例管理一个 Server 连接。
"""
from __future__ import annotations

import time
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.mcp.protocol import (
    MCPServerState,
    MCPInitializeResult,
    MCPTool,
    MCPToolResult,
    build_error_response,
    build_notification,
    build_request,
    build_response,
    extract_content_parts,
    is_response,
    parse_message,
)
from mokioclaw.mcp.sandbox import SandboxPolicy
from mokioclaw.mcp.transport import MCPTransport

logger = get_logger(__name__)

# initialize 超时时间（秒）
_INIT_TIMEOUT = 15.0
# 工具调用默认超时
_CALL_TIMEOUT = 60.0


class MCPClient:
    """MCP Server 客户端

    管理单个 MCP Server 的连接生命周期：
    1. connect() — 启动子进程，发送 initialize
    2. initialize() — 握手，获取 server capabilities + tools
    3. call_tool() — 调用工具（经沙箱验证）
    4. disconnect() — 清理
    """

    def __init__(
        self,
        name: str,
        transport: MCPTransport,
        sandbox_policy: SandboxPolicy | None = None,
    ) -> None:
        self.name = name
        self._transport = transport
        self._policy = sandbox_policy
        self._state = MCPServerState.DISCONNECTED
        self._tools: list[MCPTool] = []
        self._resources: list[dict[str, Any]] = []
        self._capabilities: dict[str, Any] = {}
        self._server_info: dict[str, Any] = {}
        self._request_counter = 0

    @property
    def state(self) -> MCPServerState:
        return self._state

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities)

    def connect(self) -> bool:
        """连接到 MCP Server 并完成 initialize 握手

        Returns:
            是否成功连接
        """
        try:
            self._transport.connect()
            self._state = MCPServerState.INITIALIZING
            return self._initialize()
        except Exception as exc:
            logger.error("MCP client '%s' connect failed: %s", self.name, exc)
            self._state = MCPServerState.FAILED
            return False

    def _initialize(self) -> bool:
        """发送 initialize 请求并等待响应"""
        req_id = self._next_id()
        params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": False},
                "sampling": {},
            },
            "clientInfo": {
                "name": "dot_agent",
                "version": "0.1.0",
            },
        }
        self._transport.send(build_request("initialize", params, req_id))

        # 等待响应
        deadline = time.time() + _INIT_TIMEOUT
        while time.time() < deadline:
            msg = self._transport.receive(timeout=deadline - time.time())
            if msg is None:
                break
            if not isinstance(msg, dict):
                continue
            if not is_response(msg):
                continue
            if str(msg.get("id")) != str(req_id):
                continue
            if "error" in msg:
                logger.error("MCP initialize error: %s", msg["error"])
                self._state = MCPServerState.FAILED
                return False
            result = msg.get("result", {})
            self._capabilities = result.get("capabilities", {})
            self._server_info = result.get("serverInfo", {})
            self._state = MCPServerState.CONNECTED
            logger.info(
                "MCP client '%s' connected to %s v%s",
                self.name,
                self._server_info.get("name", "?"),
                self._server_info.get("version", "?"),
            )
            # 发送 initialized 通知
            self._transport.send(build_notification("notifications/initialized"))
            return True

        logger.error("MCP initialize timeout for '%s'", self.name)
        self._state = MCPServerState.FAILED
        return False

    def list_tools(self) -> list[MCPTool]:
        """获取 server 支持的工具列表

        自动缓存，后续调用返回缓存结果。
        """
        if self._tools:
            return list(self._tools)

        req_id = self._next_id()
        self._transport.send(build_request("tools/list", {}, req_id))

        deadline = time.time() + _CALL_TIMEOUT
        while time.time() < deadline:
            msg = self._transport.receive(timeout=deadline - time.time())
            if msg is None:
                break
            if not isinstance(msg, dict):
                continue
            if not is_response(msg):
                continue
            if str(msg.get("id")) != str(req_id):
                continue
            if "error" in msg:
                logger.error("MCP tools/list error: %s", msg["error"])
                return []
            result = msg.get("result", {})
            raw_tools = result.get("tools", [])
            self._tools = [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                    server_name=self.name,
                    annotations=t.get("annotations", {}),
                )
                for t in raw_tools
                if t.get("name")
            ]
            return list(self._tools)

        return []

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """调用 MCP 工具（含沙箱检查）

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            MCPToolResult 包含执行结果
        """
        # 1. 沙箱检查（递归检查所有嵌套的字符串值）
        if self._policy:
            try:
                for key, value in arguments.items():
                    _check_sandbox_value(self._policy, key, value)
            except SandboxBlockedError as exc:
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Sandbox blocked: {exc}"}],
                    is_error=True,
                )

        # 2. 发送工具调用请求
        req_id = self._next_id()
        params = {
            "name": tool_name,
            "arguments": arguments,
        }
        self._transport.send(build_request("tools/call", params, req_id))

        # 3. 等待响应
        deadline = time.time() + _CALL_TIMEOUT
        while time.time() < deadline:
            msg = self._transport.receive(timeout=deadline - time.time())
            if msg is None:
                break
            if not isinstance(msg, dict):
                continue
            if not is_response(msg):
                continue
            if str(msg.get("id")) != str(req_id):
                continue

            if "error" in msg:
                error = msg["error"]
                return MCPToolResult(
                    content=[{"type": "text", "text": f"MCP error: {error.get('message', 'unknown')}"}],
                    is_error=True,
                )

            result = msg.get("result", {})
            content = result.get("content", [])
            is_error = bool(result.get("isError", False))
            return MCPToolResult(content=content, is_error=is_error)

        return MCPToolResult(
            content=[{"type": "text", "text": f"Tool call timeout after {_CALL_TIMEOUT}s"}],
            is_error=True,
        )

    def ping(self) -> bool:
        """检查 server 是否存活"""
        req_id = self._next_id()
        self._transport.send(build_request("ping", {}, req_id))

        deadline = time.time() + 5.0
        while time.time() < deadline:
            msg = self._transport.receive(timeout=deadline - time.time())
            if msg is None:
                break
            if not isinstance(msg, dict):
                continue
            if is_response(msg) and str(msg.get("id")) == str(req_id):
                return "error" not in msg
        return False

    def disconnect(self) -> None:
        """断开连接"""
        self._transport.disconnect()
        self._state = MCPServerState.DISCONNECTED
        self._tools = []

    def _next_id(self) -> str:
        self._request_counter += 1
        return f"{self._request_counter}"


# ============================================================
# 辅助函数
# ============================================================

def _looks_like_path(value: str) -> bool:
    """检查字符串是否看起来像文件路径"""
    # 包含路径分隔符
    if "/" in value or "\\" in value:
        return True
    # 以 ~ 开头（home 目录）
    if value.startswith("~"):
        return True
    # 以 ./ 或 ../ 开头（相对路径）
    if value.startswith("./") or value.startswith("../"):
        return True
    # 以 / 开头（绝对路径）
    if value.startswith("/") or re.match(r'^[A-Za-z]:[/\\]', value):
        return True
    # 包含扩展名且看起来像文件名（避免误判版本号如 v2.0.1）
    if "." in value and len(value) < 100:
        # 检查是否像文件名：最后一个点后是常见扩展名
        parts = value.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) <= 5 and parts[1].isalnum():
            return True
    return False


def _looks_like_command(value: str) -> bool:
    """检查字符串是否看起来像命令或包含 shell 元字符"""
    value = value.strip()
    if not value:
        return False
    first_word = value.split()[0]
    if first_word.startswith("$"):
        return True
    if first_word in ("rm", "rmdir", "sudo", "curl", "wget", "python", "bash", "sh", "cmd"):
        return True
    # 检测 shell 元字符注入
    _SHELL_METACHARS = (";", "|", "&", "$(", "`", "&&", "||", ">", "<")
    if any(meta in value for meta in _SHELL_METACHARS):
        return True
    return False


def _check_sandbox_value(policy: Any, key: str, value: Any) -> None:
    """递归检查值是否违反沙箱策略"""
    if isinstance(value, dict):
        for k, v in value.items():
            _check_sandbox_value(policy, k, v)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_sandbox_value(policy, f"{key}[{i}]", item)
    elif isinstance(value, str):
        if _looks_like_path(value):
            allowed, reason = policy.check_file_access(value)
            if not allowed:
                raise SandboxBlockedError(reason)
        if _looks_like_command(value):
            allowed, reason = policy.check_command(value)
            if not allowed:
                raise SandboxBlockedError(reason)


class SandboxBlockedError(Exception):
    """沙箱拦截异常"""
    pass
