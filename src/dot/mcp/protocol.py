"""
MCP (Model Context Protocol) 协议核心

实现 MCP 协议的 JSON-RPC 消息格式、协议常量和状态机。

协议参考：https://modelcontextprotocol.io/specification/2024-11-05/
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MCPServerState(Enum):
    """MCP Server 连接状态"""
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    CONNECTED = "connected"
    FAILED = "failed"


# MCP 协议版本
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPTool:
    """MCP 工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_langchain_tool(self) -> dict[str, Any]:
        """转换为 LangChain StructuredTool 参数字典"""
        return {
            "name": self.name,
            "description": self.description,
            "args_schema": None,  # 需要时可由 input_schema 生成 Pydantic model
            "func": None,  # 由 MCPClient 注入
        }


@dataclass
class MCPResource:
    """MCP 资源定义"""
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass
class MCPPrompt:
    """MCP Prompt 定义"""
    name: str
    description: str
    arguments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MCPInitializeResult:
    """initialize 响应结果"""
    protocol_version: str
    capabilities: dict[str, Any]
    server_info: dict[str, Any]


@dataclass
class MCPToolResult:
    """tools/call 响应结果"""
    content: list[dict[str, Any]]
    is_error: bool = False


# ============================================================
# JSON-RPC 消息构建/解析
# ============================================================

def build_request(method: str, params: dict[str, Any] | None = None, request_id: str | int | None = None) -> dict[str, Any]:
    """构建 JSON-RPC 请求消息"""
    msg: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id if request_id is not None else _gen_id(),
        "method": method,
    }
    if params is not None:
        msg["params"] = params
    return msg


def build_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """构建 JSON-RPC 通知消息（无 id，不需要响应）"""
    msg: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        msg["params"] = params
    return msg


def build_response(result: Any, request_id: str | int) -> dict[str, Any]:
    """构建 JSON-RPC 成功响应"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def build_error_response(request_id: str | int, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """构建 JSON-RPC 错误响应"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


def parse_message(raw: str) -> dict[str, Any]:
    """解析 JSON-RPC 消息"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON-RPC message: {exc}") from exc


def is_request(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" in msg


def is_response(msg: dict[str, Any]) -> bool:
    return "result" in msg or "error" in msg


def is_notification(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" not in msg


# ============================================================
# MCP 方法名常量
# ============================================================

METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
METHOD_RESOURCES_LIST = "resources/list"
METHOD_RESOURCES_READ = "resources/read"
METHOD_PROMPTS_LIST = "prompts/list"
METHOD_PROMPTS_GET = "prompts/get"
METHOD_PING = "ping"
METHOD_CANCELLED = "notifications/cancelled"


# ============================================================
# 工具函数
# ============================================================

def _gen_id() -> str:
    return str(uuid.uuid4())


def extract_content_parts(result: MCPToolResult | dict[str, Any]) -> tuple[str, list[str]]:
    """从工具结果中提取文本内容和附件路径"""
    if isinstance(result, MCPToolResult):
        items = result.content
    elif isinstance(result, dict):
        items = result.get("content", [])
    else:
        return "", []

    texts: list[str] = []
    attachments: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("uri", ""))
        if uri:
            attachments.append(uri)
            continue
        mime = str(item.get("mimeType", "")).lower()
        if mime.startswith("text/") or mime in ("application/json", ""):
            texts.append(str(item.get("text", item.get("content", ""))))
        else:
            # 非文本类型也尝试提取
            texts.append(str(item.get("text", "")))
    return "\n".join(texts), attachments
