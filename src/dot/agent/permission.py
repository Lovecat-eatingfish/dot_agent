"""
dot.agent.permission — 权限契约（agent 层定义，coding 层实现）

agent 循环在工具执行前需要权限裁决。为避免 agent → coding 的反向依赖，
这里只定义契约：

- Decision            ：裁决三态（ALLOW / ASK / DENY）
- PermissionDecision  ：裁决结果载体（来源 + 原因 + 用户可见消息）
- PermissionGate      ：权限检查协议（Protocol，由 dot.coding.permission.PermissionManager 实现）

具体三级拦截逻辑（系统黑名单 → 项目黑名单 → 模式规则）在 coding 层实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

SECURITY_CONFIG_FILE = ".agent-security.json"


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionDecision:
    decision: Decision
    source: str = ""  # system | project | mode
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def deny_message(self) -> str:
        if self.source == "system":
            return f"Blocked by system security rule: {self.reason}"
        if self.source == "project":
            return f"Blocked by project {SECURITY_CONFIG_FILE} rule: {self.reason}"
        return f"Blocked by current mode: {self.reason}"


class PermissionGate(Protocol):
    """权限检查协议（coding 层的 PermissionManager 满足该协议，无需显式继承）"""

    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_mode: str = "auto",
        approved: bool = False,
    ) -> PermissionDecision:
        """三级权限校验"""
        ...

    def ask_user(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: PermissionDecision,
        *,
        agent_mode: str = "",
    ) -> bool | Awaitable[bool]:
        """发起人工审批；无交互能力时自动降级 DENY"""
        ...


# ask_user 审批回调签名（由 UI 层注入）
ApprovalHandler = Callable[[dict[str, Any]], bool | Awaitable[bool]]
