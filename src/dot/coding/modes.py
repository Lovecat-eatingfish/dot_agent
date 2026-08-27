"""
dot.coding.modes — AgentMode 枚举

三种模式控制工具使用权限，通过 /mode 命令热切换。
"""
from __future__ import annotations

from enum import Enum


class AgentMode(Enum):
    """Agent 运行模式"""
    PLAN = "plan"
    EDIT = "edit"
    AUTO = "auto"

    @classmethod
    def from_str(cls, value: str) -> AgentMode:
        """从字符串解析模式（不区分大小写）"""
        try:
            return cls(value.lower())
        except ValueError:
            return cls.AUTO

    @property
    def label(self) -> str:
        return self.value.upper()

    def allows_file_read(self) -> bool:
        """是否允许文件读取"""
        return True  # 所有模式都允许读取

    def allows_file_write(self) -> str:
        """文件写入权限：ALLOW / ASK"""
        if self == AgentMode.PLAN:
            return "ASK"
        return "ALLOW"

    def allows_bash(self) -> str:
        """Bash 权限：ALLOW / ASK / DENY"""
        if self == AgentMode.PLAN:
            return "DENY"
        if self == AgentMode.EDIT:
            return "ASK"
        return "ALLOW"

    def allows_mcp(self) -> bool:
        """是否允许 MCP 工具"""
        return True  # 所有模式都允许 MCP
