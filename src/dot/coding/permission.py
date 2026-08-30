"""
dot.coding.permission — PermissionManager 权限管控

三级拦截（绝对顺序，前面命中则后续不执行）：
  1. 系统内置黑名单（最高优先级，不可覆盖/关闭）
  2. 项目自定义黑名单（.agent-security.json）
  3. 会话运行模式规则（Plan / Edit / Auto）

决策三态：ALLOW / ASK / DENY。
ASK 审批：有 UI 时弹确认框，无 UI 时自动降级 DENY（无头兜底）。
权限检查在 hooks 之前执行，不可被用户扩展。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from enum import Enum
from inspect import isawaitable
from pathlib import Path
from typing import Any, Callable

from .modes import AgentMode

logger = logging.getLogger(__name__)
SECURITY_CONFIG_FILE = ".agent-security.json"

FILE_TOOLS = {"read_file", "write_file", "edit_file", "glob_search", "grep"}
FILE_WRITE_TOOLS = {"write_file", "edit_file"}
_PATH_ARGS = ("file_path", "path", "pattern_path")


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


@dataclass
class ProjectSecurityConfig:
    """项目自定义安全规则"""
    deny_file_patterns: list[str] = field(default_factory=list)
    deny_bash_regex: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @classmethod
    def load(cls, workspace: Path) -> ProjectSecurityConfig:
        path = workspace / SECURITY_CONFIG_FILE
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        config = cls(source_path=path)
        patterns = data.get("denyFilePatterns")
        if isinstance(patterns, list):
            config.deny_file_patterns = [str(p) for p in patterns if isinstance(p, str)]
        regexes = data.get("denyBashRegex")
        if isinstance(regexes, list):
            config.deny_bash_regex = [r for r in regexes if isinstance(r, str)]
        return config

    def match_file(self, rel_path: str) -> str | None:
        from fnmatch import fnmatch
        normalized = rel_path.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        if not normalized:
            return None
        for pattern in self.deny_file_patterns:
            candidates = [pattern]
            if pattern.startswith("**/"):
                # globstar 语义：**/ 匹配任意深度，含顶层文件本身
                candidates.append(pattern[len("**/"):])
            if any(fnmatch(normalized, c) for c in candidates):
                return pattern
        return None

    def match_bash(self, command: str) -> str | None:
        for pattern in self.deny_bash_regex:
            try:
                if re.search(pattern, command):
                    return pattern
            except re.error:
                continue
        return None


class PermissionManager:
    """全局权限管理器

    所有工具调用必经 check()；三级拦截顺序不可改动。
    """

    def __init__(self) -> None:
        self._project = ProjectSecurityConfig()
        self._workspace: Path | None = None
        self._approval_handler: Callable[[dict[str, Any]], bool] | None = None

    def load_project(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._project = ProjectSecurityConfig.load(self._workspace)

    def set_approval_handler(self, handler: Callable[[dict[str, Any]], bool] | None) -> None:
        self._approval_handler = handler

    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_mode: str = "auto",
        approved: bool = False,
    ) -> PermissionDecision:
        """三级权限校验"""
        try:
            return self._check_inner(tool_name, args, agent_mode, approved)
        except Exception:
            return PermissionDecision(Decision.DENY, "system", "check-internal-error")

    def _check_inner(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_mode: str,
        approved: bool,
    ) -> PermissionDecision:
        # MCP / Skill 工具不做权限校验（直接放行）
        if tool_name.startswith(("mcp_", "skill_")):
            return PermissionDecision(Decision.ALLOW, "mode", "mcp/skill not gated")

        ws = self._workspace or Path.cwd()

        # ---- 1. 系统内置黑名单 ----
        if tool_name in FILE_TOOLS:
            path_str = self._extract_path(args)
            if path_str:
                try:
                    candidate = Path(path_str).expanduser()
                    if not candidate.is_absolute():
                        candidate = ws / candidate
                    resolved = candidate.resolve()
                    ws_resolved = ws.resolve()
                    # 使用 is_relative_to 做可靠的路径遍历检查
                    if not resolved.is_relative_to(ws_resolved):
                        return PermissionDecision(Decision.DENY, "system", "path traversal blocked")
                except Exception:
                    logger.error(f"Error resolving path: {path_str}")

        # ---- 2. 项目自定义黑名单 ----
        if tool_name == "bash":
            hit = self._project.match_bash(str(args.get("command", "")))
            if hit:
                return PermissionDecision(Decision.DENY, "project", f"denyBashRegex: {hit}")

        if tool_name in FILE_TOOLS:
            path_str = self._extract_path(args)
            if path_str:
                rel = self._to_rel(path_str, ws)
                hit = self._project.match_file(rel)
                if hit:
                    return PermissionDecision(Decision.DENY, "project", f"denyFilePatterns: {hit}")

        # ---- 3. 模式规则 ----
        mode = AgentMode.from_str(agent_mode)
        if approved:
            return PermissionDecision(Decision.ALLOW, "mode", f"approved-once ({mode.value})")

        if mode == AgentMode.PLAN:
            if tool_name == "bash":
                return PermissionDecision(Decision.DENY, "mode", "plan mode: bash denied")
            if tool_name in FILE_WRITE_TOOLS:
                return PermissionDecision(Decision.ASK, "mode", "plan mode: file write needs approval")
            return PermissionDecision(Decision.ALLOW, "mode", "plan mode read-only")

        if mode == AgentMode.EDIT:
            if tool_name == "bash":
                return PermissionDecision(Decision.ASK, "mode", "edit mode: bash needs approval")
            return PermissionDecision(Decision.ALLOW, "mode", "edit mode")

        return PermissionDecision(Decision.ALLOW, "mode", "auto mode")

    async def ask_user(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: PermissionDecision,
        *,
        agent_mode: str = "",
    ) -> bool | Awaitable[bool]:
        """发起人工审批；无交互能力时自动降级 DENY"""
        info = {
            "tool_name": tool_name,
            "agent_mode": agent_mode,
            "source": decision.source,
            "reason": decision.reason,
            "args": dict(args),
        }
        handler = self._approval_handler
        if handler is None:
            return False
        try:
            result = handler(info)
            if isawaitable(result):
                result = await result
            return bool(result)
        except (EOFError, KeyboardInterrupt):
            return False
        except Exception:
            return False

    @staticmethod
    def _extract_path(args: dict[str, Any]) -> str:
        for key in _PATH_ARGS:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _to_rel(path_str: str, workspace: Path) -> str:
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            return str(p).replace("\\", "/")
        try:
            return str(p.resolve().relative_to(workspace.resolve())).replace("\\", "/")
        except ValueError:
            return str(p).replace("\\", "/")


# 全局单例
_manager: PermissionManager | None = None


def get_permission_manager() -> PermissionManager:
    global _manager
    if _manager is None:
        _manager = PermissionManager()
    return _manager


def reset_permission_manager() -> None:
    global _manager
    _manager = None
