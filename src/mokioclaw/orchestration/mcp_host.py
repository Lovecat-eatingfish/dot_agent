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

from dot.core import get_logger

# ============================================================
# MCP Host
# ============================================================
logger = get_logger(__name__)


class MCPHost:
    """MCP 工具渐进披露 Host

    维护 mcp_ 前缀映射表、工具 schema 内存缓存、已加载工具集合。
    """

    def __init__(self, mcp_manager: Any) -> None:
        """初始化 MCP Host

        Args:
            mcp_manager: MCPManager 实例，用于对接 MCPBridge
        """
        self._mcp_manager = mcp_manager
        # mcp_prefixed_name → original_name 映射
        self._name_map: dict[str, str] = {}
        # mcp_prefixed_name → 完整 schema 缓存
        self._schema_cache: dict[str, dict[str, Any]] = {}
        # 所有可用 mcp_ 工具名列表（按发现顺序）
        self._all_tool_names: list[str] = []
        self._server_map: dict[str, str] = {}
        # 已加载到 LLM tools 列表中的 mcp_ 工具名集合
        self._loaded_tools: set[str] = set()
        # 工具目录纯文本（system prompt 注入用）
        self._catalog_text: str = ""

    #
    # def discover_tools(self) -> list[str]:
    #     """发现所有 MCP 工具，建立前缀映射和 schema 缓存
    #
    #     Returns:
    #         带 mcp_ 前缀的工具名列表
    #     """
    #     self._name_map.clear()
    #     self._schema_cache.clear()
    #     self._all_tool_names.clear()
    #
    #     try:
    #         mcp_tools = self._mcp_manager.list_tools()
    #     except Exception:
    #         mcp_tools = []
    #
    #     for tool in mcp_tools:
    #         original_name = getattr(tool, "name", "") or ""
    #         if not original_name:
    #             continue
    #         # 避免双重前缀
    #         if original_name.startswith("mcp_"):
    #             prefixed = original_name
    #         else:
    #             prefixed = f"mcp_{original_name}"
    #         self._name_map[prefixed] = original_name
    #         # 缓存完整 schema
    #         schema = self._build_tool_schema(tool, original_name, prefixed)
    #         self._schema_cache[prefixed] = schema
    #         self._all_tool_names.append(prefixed)
    #
    #     self._rebuild_catalog()
    #     return list(self._all_tool_names)


def discover_tools(self) -> list[str]:
    """发现所有 MCP 工具，建立前缀映射和 schema 缓存
    每次调用都会重置全部映射、缓存，重新从mcp_manager拉取工具列表
    """
    # ✅全部重置逻辑写在 discover_tools 内部
    self._name_map.clear()
    self._server_map.clear()
    self._schema_cache.clear()
    self._all_tool_names.clear()
    self._loaded_tools.clear()  # 重新扫描工具时，清空已加载状态
    self._catalog_text = ""

    try:
        mcp_tools = self._mcp_manager.list_tools()
    except Exception as e:
        logger.warning("discover mcp tools failed", exc_info=True)
        mcp_tools = []

    for tool in mcp_tools:
        original_name = getattr(tool, "name", "") or ""
        server_name = getattr(tool, "server_name", "") or ""
        if not original_name or not server_name:
            continue

        if original_name.startswith("mcp_"):
            prefixed = original_name
        else:
            prefixed = f"mcp_{server_name}_{original_name}"

        self._name_map[prefixed] = original_name
        self._server_map[prefixed] = server_name

        schema = self._build_tool_schema(tool, original_name, prefixed)
        self._schema_cache[prefixed] = schema
        self._all_tool_names.append(prefixed)

    self._rebuild_catalog()
    return list(self._all_tool_names)

    def get_loaded_tools(self) -> list[str]:
        """获取当前已加载的 mcp_ 工具名列表"""
        return sorted(self._loaded_tools)

    def get_all_tool_names(self) -> list[str]:
        """获取所有可用 mcp_ 工具名列表"""
        return list(self._all_tool_names)

    def get_catalog_text(self) -> str:
        """获取工具目录纯文本（用于注入 system prompt）"""
        return self._catalog_text

    def load_tool_schema(self, tool_name: str) -> dict[str, Any]:
        """加载指定工具的完整 schema，标记为已加载

        Args:
            tool_name: 带 mcp_ 前缀的工具名

        Returns:
            工具完整 schema 字典

        Raises:
            ValueError: 工具不存在
        """
        if tool_name not in self._schema_cache:
            raise ValueError(f"工具 {tool_name} 不存在，请查看 所有 MCP 工具目录")
        self._loaded_tools.add(tool_name)
        return self._schema_cache[tool_name]

    def is_tool_loaded(self, tool_name: str) -> bool:
        """检查工具是否已加载"""
        return tool_name in self._loaded_tools

    def resolve_original_name(self, prefixed_name: str) -> str:
        """把 mcp_ 前缀名还原为原始工具名"""
        return self._name_map.get(prefixed_name, prefixed_name)

    def get_loaded_schemas(self) -> list[dict[str, Any]]:
        """获取所有已加载工具的完整 schema 列表（供 LLM tools 数组使用）"""
        result = []
        for name in self._all_tool_names:
            if name in self._loaded_tools:
                schema = self._schema_cache.get(name, {})
                if schema:
                    result.append(schema)
        return result

    def get_system_prompt_rules(self) -> str:
        """获取 MCP 工具调用规则文本（注入 system prompt）"""
        return """MCP 外部工具调用规则
所有以 mcp_ 为前缀的工具均为延迟加载的 MCP 外部工具，不可以直接调用。
如果你想要调用任意 mcp_ 开头的工具，必须严格分两步执行：
1. 第一步：调用 search_available_tools，获取可用 MCP 工具列表（name、描述）
2. 第二步：基于 search_available_tools 返回结果，调用 load_tool_schema(tool_name="mcp_xxx")
当 load_tool_schema 返回成功后，才允许调用该 mcp_* 工具。

禁止跳过步骤直接输出 mcp_* 的工具调用。若你直接调用未加载的mcp_*工具会被系统拦截。

元工具（控制工具）：
- search_available_tools：浏览全部 MCP 工具目录
- load_tool_schema：加载指定 mcp_ 工具完整 schema，使其变为可调用
"""

    def get_meta_tools(self) -> list[dict[str, Any]]:
        """获取元工具定义（仅注册到 LLM tools 数组的两个工具）

        Returns:
            元工具 schema 列表
        """
        return [
            {
                "name": "search_available_tools",
                "description": "浏览全部可用 MCP 外部工具目录，返回工具名称和简短描述。必须先调用此方法查看可用工具，再调用 load_tool_schema 加载具体工具。",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "load_tool_schema",
                "description": "加载指定 MCP 工具的完整 schema，使其变为可调用状态。参数 tool_name 必须是 search_available_tools 返回的工具名。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "要加载的 mcp_ 工具名称，如 mcp_fs_read_file",
                        }
                    },
                    "required": ["tool_name"],
                },
            },
        ]

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _rebuild_catalog(self) -> None:
        """重建工具目录纯文本"""
        lines = ["可用 MCP 外部工具目录：", ""]
        for name in self._all_tool_names:
            schema = self._schema_cache.get(name, {})
            desc = schema.get("description", "无描述")
            # 截断过长描述
            if len(desc) > 80:
                desc = desc[:77] + "..."
            loaded_mark = " [已加载]" if name in self._loaded_tools else ""
            lines.append(f"- {name}: {desc}{loaded_mark}")
        lines.append("")
        lines.append(self.get_system_prompt_rules())
        self._catalog_text = "\n".join(lines)

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
        """执行 MCP 工具调用

        Args:
            call: 工具调用字典，包含 name 和 args

        Returns:
            工具执行结果
        """
        tool_name = call.get("name", "")
        args = call.get("args", {}) or {}

        # 防御拦截：未加载的 mcp_ 工具直接拦截
        if tool_name.startswith("mcp_") and not self._host.is_tool_loaded(tool_name):
            return {
                "ok": False,
                "error": (
                    f"不能直接调用 mcp_* 工具 {tool_name}，"
                    "请先调用 search_available_tools，再调用 load_tool_schema 加载工具 schema"
                ),
            }

        # 剥前缀还原原始工具名
        original_name = self._host.resolve_original_name(tool_name)

        # 转发给 MCP Manager
        try:
            result = self._host._mcp_manager.call_tool(
                f"{self._host._resolve_server_name(tool_name)}:{original_name}",
                args,
            )
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def handle_meta_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        """处理元工具调用（search_available_tools / load_tool_schema）

        Args:
            call: 工具调用字典

        Returns:
            元工具执行结果
        """
        tool_name = call.get("name", "")

        if tool_name == "search_available_tools":
            return self._handle_search()

        if tool_name == "load_tool_schema":
            tool_name_arg = call.get("args", {}).get("tool_name", "")
            return self._handle_load(tool_name_arg)

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
                "loaded": name in self._host._loaded_tools,
            })
        return {"ok": True, "tools": tools}

    def _handle_load(self, tool_name: str) -> dict[str, Any]:
        """处理 load_tool_schema"""
        try:
            schema = self._host.load_tool_schema(tool_name)
            return {
                "ok": True,
                "tool_name": tool_name,
                "description": schema.get("description", ""),
                "message": f"工具 {tool_name} 已加载，现在可以调用。",
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
