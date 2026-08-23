"""
MCP Host 渐进披露（Tool-Search 模式）

职责：
- 对接 MCPBridge，获取全部工具 schema 缓存到内存
- 对外暴露带 mcp_ 前缀的工具名，生成纯文本目录注入 system prompt
- 仅注册 2 个元工具：search_available_tools / load_tool_schema
- 运行时防御拦截：未加载的 mcp_* 工具直接拦截

名字映射逻辑：
- MCP Server 原始工具名 → mcp_{server}_{tool}（统一加 mcp_ 前缀）
- 转发 RPC 时剥掉前缀还原原始名
- 内部维护 mcp_prefixed_name → original_name 映射表

关键时序：
1. session 初始化：Host 调用 MCPBridge 获取全部工具 schema → 内存缓存 → 生成目录文本
2. 每次 LLM 请求前：Host 把目录文本 + 调用规则注入 system prompt
3. LLM 先调用 search_available_tools 浏览目录（可选，因为目录已在 prompt 中）
4. LLM 调用 load_tool_schema("mcp_xxx") → Host 把该工具完整 schema 加入 loaded 集合
5. LLM 输出 mcp_xxx 的 tool_use → Host 剥前缀 → 转发 MCP Server
"""
from __future__ import annotations

from typing import Any, Optional


# ============================================================
# MCP Host
# ============================================================

class MCPHost:
    """MCP 工具渐进披露 Host

    维护 mcp_ 前缀映射表、工具 schema 内存缓存、已加载工具集合。
    """

    def __init__(self,  mcp_manager: Any) -> None:
        """初始化 MCP Host

        Args:
            mcp_manager: MCPManager 实例，用于对接 MCPBridge
        """
        self._mcp_manager = mcp_manager
        # mcp_prefixed_name → original_name 映射
        self._name_map: dict[str, str] = {}
        # mcp_prefixed_name → server_name 映射（转发 RPC 用，避免正则猜）
        self._server_map: dict[str, str] = {}
        # mcp_prefixed_name → 完整 schema 缓存
        self._schema_cache: dict[str, dict[str, Any]] = {}
        # 工具目录纯文本（基础目录，无 [已加载] 标记；system prompt 注入用）
        self._catalog_text: str = ""
        # 所有可用 mcp_ 工具名列表（按发现顺序）
        self._all_tool_names: list[str] = []

    def discover_tools(self) -> list[str]:
        """发现所有 MCP 工具，建立前缀映射和 schema 缓存

        命名约定（对齐 fix.md）：mcp_{server_name}_{tool_name}，
        server 名编入前缀避免不同 server 的同名工具冲突。

        Returns:
            带 mcp_ 前缀的工具名列表
        """
        self._name_map.clear()
        self._server_map.clear()
        self._schema_cache.clear()
        self._all_tool_names.clear()
        self._catalog_text = ""

        try:
            mcp_tools = self._mcp_manager.list_tools()
        except Exception:
            mcp_tools = []

        for tool in mcp_tools:
            original_name = getattr(tool, "name", "") or ""
            if not original_name:
                continue
            server_name = getattr(tool, "server_name", "") or ""
            # 避免双重前缀
            if original_name.startswith("mcp_"):
                prefixed = original_name
            elif server_name:
                prefixed = f"mcp_{server_name}_{original_name}"
            else:
                prefixed = f"mcp_{original_name}"
            self._name_map[prefixed] = original_name
            self._server_map[prefixed] = server_name
            # 缓存完整 schema
            schema = self._build_tool_schema(tool, original_name, prefixed)
            self._schema_cache[prefixed] = schema
            self._all_tool_names.append(prefixed)

        self._rebuild_catalog()
        return list(self._all_tool_names)


    def get_all_tool_names(self) -> list[str]:
        """获取所有可用 mcp_ 工具名列表"""
        return list(self._all_tool_names)

    def close(self) -> None:
        """关闭底层 MCP 连接（session 销毁/切换时调用）"""
        shutdown = getattr(self._mcp_manager, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def get_catalog_text(self, loaded_names: set[str] | None = None) -> str:
        """获取工具目录纯文本（用于注入 system prompt）

        Args:
            loaded_names: 已加载工具名集合；None=基础目录（无 [已加载] 标记），
                传入则返回带 [已加载] 标记的 per-session 视图。
        """
        if loaded_names is None:
            return self._catalog_text
        return self._build_catalog_with_marks(loaded_names)

    def load_tool_schema(self, tool_name: str) -> dict[str, Any]:
        """返回指定工具的完整 schema（共享，不追踪 per-session 加载状态）

        Args:
            tool_name: 带 mcp_ 前缀的工具名

        Returns:
            工具完整 schema 字典

        Raises:
            ValueError: 工具不存在
        """
        if tool_name not in self._schema_cache:
            raise ValueError(f"工具 {tool_name} 不存在，请查看 skill 目录")
        return self._schema_cache[tool_name]

    def resolve_original_name(self, prefixed_name: str) -> str:
        """把 mcp_ 前缀名还原为原始工具名"""
        return self._name_map.get(prefixed_name, prefixed_name)

    def get_loaded_schemas(self, loaded_names: set[str]) -> list[dict[str, Any]]:
        """获取指定已加载工具的完整 schema 列表（供 LLM tools 数组使用）

        Args:
            loaded_names: per-session 已加载工具名集合
        """
        result = []
        for name in self._all_tool_names:
            if name in loaded_names:
                schema = self._schema_cache.get(name, {})
                if schema:
                    result.append(schema)
        return result

    def get_system_prompt_rules(self) -> str:
        """获取 MCP 工具调用规则文本（注入 system prompt）"""
        return """MCP 外部工具调用规则
所有以 mcp_ 为前缀的工具均为延迟加载的 MCP 外部工具，命名格式 mcp_{server}_{tool}。
如果你想要调用任意 mcp_ 开头的工具，必须先调用 mcp_search(tool_name="mcp_xxx")
获取该工具的完整定义（description + input_schema），按定义构造参数后再调用。

未加载直接调用 mcp_* 工具时，系统会把该工具的定义返回给你而不是执行——
看到定义后请按 schema 重新发起调用。

元工具（控制工具）：
- mcp_search：查 MCP 工具目录（无参数返回全部列表）；带 tool_name 返回完整定义并加载
"""

    def get_meta_tools(self) -> list[dict[str, Any]]:
        """获取元工具定义（mcp_search 由 tools/meta.py 构建注册，此处仅保留 schema 说明）"""
        return [
            {
                "name": "mcp_search",
                "description": "搜索 MCP 工具：不带参数返回全部工具目录（name + 描述）；带 tool_name 返回该工具完整定义（description + input_schema）并加载为可调用。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "要加载的 mcp_ 工具名称，如 mcp_fs_read_file",
                        }
                    },
                },
            },
        ]

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _rebuild_catalog(self) -> None:
        """重建基础工具目录纯文本（无 [已加载] 标记，共享）"""
        self._catalog_text = self._render_catalog(loaded_names=None)

    def _build_catalog_with_marks(self, loaded_names: set[str]) -> str:
        """生成带 [已加载] 标记的 per-session 目录视图"""
        return self._render_catalog(loaded_names=loaded_names)

    def _render_catalog(self, *, loaded_names: set[str] | None) -> str:
        lines = ["可用 MCP 外部工具目录：", ""]
        for name in self._all_tool_names:
            schema = self._schema_cache.get(name, {})
            desc = schema.get("description", "无描述")
            # 截断过长描述
            if len(desc) > 80:
                desc = desc[:77] + "..."
            loaded_mark = " [已加载]" if (loaded_names and name in loaded_names) else ""
            lines.append(f"- {name}: {desc}{loaded_mark}")
        lines.append("")
        lines.append(self.get_system_prompt_rules())
        return "\n".join(lines)

    def _build_tool_schema(
        self, tool: Any, original_name: str, prefixed_name: str
    ) -> dict[str, Any]:
        """从 MCPTool 构建标准工具 schema"""
        desc = getattr(tool, "description", "") or ""
        # 尝试从 tool 对象提取 input_schema
        input_schema = {}
        if hasattr(tool, "input_schema") and tool.input_schema:
            input_schema = tool.input_schema
        elif hasattr(tool, "parameters") and tool.parameters:
            input_schema = tool.parameters
        else:
            # 最小 schema 占位
            input_schema = {"type": "object", "properties": {}}

        return {
            "name": prefixed_name,
            "description": desc,
            "input_schema": input_schema,
            "_original_name": original_name,
        }


# ============================================================
# MCP Tool Executor Wrapper（运行时拦截 + 转发）
# ============================================================

class MCPToolExecutor:
    """MCP 工具执行包装器

    运行时拦截 mcp_* 工具调用：
    - 已加载 → 剥前缀转发 MCP Server
    - 未加载 → 返回拦截错误
    """

    def __init__(self, mcp_host: MCPHost) -> None:
        self._host = mcp_host

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """执行 MCP 工具调用（已加载的工具）

        Args:
            call: 工具调用字典，包含 name 和 args

        Returns:
            工具执行结果
        """
        tool_name = call.get("name", "")
        args = call.get("args", {}) or {}

        # 剥前缀还原原始工具名
        original_name = self._host.resolve_original_name(tool_name)
        # server 名优先查映射表，兜底用名称解析
        server_name = self._host._server_map.get(tool_name) or self._host._resolve_server_name(tool_name)

        # 转发给 MCP Manager
        try:
            result = self._host._mcp_manager.call_tool(
                f"{server_name}:{original_name}",
                args,
            )
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def handle_meta_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        """处理元工具调用（mcp_search）

        Args:
            call: 工具调用字典

        Returns:
            元工具执行结果
        """
        tool_name = call.get("name", "")

        if tool_name in ("mcp_search", "search_available_tools"):
            tool_name_arg = (call.get("args", {}) or {}).get("tool_name", "")
            if tool_name_arg:
                return self._handle_load(tool_name_arg)
            return self._handle_search()

        return {"ok": False, "error": f"未知元工具: {tool_name}"}

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _handle_search(self) -> dict[str, Any]:
        """处理 search_available_tools"""
        tools = []
        for name in self._host.get_all_tool_names():
            schema = self._host._schema_cache.get(name, {})
            tools.append({
                "name": name,
                "description": schema.get("description", "无描述"),
                "loaded": False,  # host 不再追踪 per-session loaded（此方法已废弃）
            })
        return {"ok": True, "tools": tools}

    def _handle_load(self, tool_name: str) -> dict[str, Any]:
        """处理 mcp_search(tool_name=...)：加载并返回完整定义"""
        try:
            schema = self._host.load_tool_schema(tool_name)
            return {
                "ok": True,
                "tool_name": tool_name,
                "description": schema.get("description", ""),
                "input_schema": schema.get("input_schema", {}),
                "message": f"工具 {tool_name} 已加载，请按 input_schema 构造参数调用。",
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def _resolve_server_name(self, prefixed_name: str) -> str:
        """从 mcp__{server}__{tool} 中提取 server 名

        支持两种格式：
        - mcp__{server}__{tool}（双下划线，标准格式）
        - mcp_{server}_{tool}（单下划线，兼容格式）
        """
        # 去掉 mcp_ 前缀
        rest = prefixed_name[4:] if prefixed_name.startswith("mcp_") else prefixed_name
        # 按 __ 分割（标准格式：mcp__{server}__{tool}）
        if rest.startswith("_"):
            parts = rest.lstrip("_").split("__")
            if len(parts) >= 2:
                return parts[0]
        # 兼容格式：mcp_{server}_{tool}，取第一个段
        parts = rest.split("_")
        if parts:
            return parts[0]
        return ""
