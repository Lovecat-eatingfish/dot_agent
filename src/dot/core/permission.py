"""
权限控制系统（doc/fix-权限控制.md）

三级拦截（绝对顺序，前面命中则后续不执行）：
  1. 系统内置黑名单（最高优先级，不可覆盖/关闭）
     - Bash：危险命令正则（rm -rf / format / shutdown 等，复用 bash_tool.DANGEROUS_PATTERNS）
     - 文件：path_security 全套底线（禁访目录 / 敏感文件 / 写目录白名单 / 防穿越）
  2. 项目自定义黑名单（.agent-security.json）
     - denyFilePatterns：glob，作用于全部文件工具（读/写/改/删）
     - denyBashRegex：正则，作用于 Bash
     - 只增不减：只能加限制，任何 allow/override 字段一律忽略
  3. 会话运行模式规则（Plan / Edit / Auto）
     - Plan：文件写 ASK，Bash DENY，其余 ALLOW
     - Edit：Bash 一律 ASK，其余 ALLOW
     - Auto：全部 ALLOW（黑名单已在前两级拦截）

决策三态：ALLOW / ASK / DENY。
ASK 审批：仅控制台场景（本地调试），Y/N 确认后带单次标记重走完整校验；
无交互能力（stdin 不可用）时自动降级 DENY（无头兜底，不卡死）。
MCP（mcp_）与 Skill（skill_）工具本期不做权限校验（用户决策）。
所有决策全程 trace 埋点（service=permission）。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .log import get_logger
from .path_security import PathSecurityError, WorkspaceNs, validate_path_access

logger = get_logger(__name__)

# 项目配置文件（按文档固定在项目根，不在 .dot/ 下）
SECURITY_CONFIG_FILE = ".agent-security.json"

# 文件类工具（读/写/改/删/检索都算，作用于 denyFilePatterns）
FILE_TOOLS = {"FileReadTool", "FileWriteTool", "FileEditTool", "GlobTool", "GrepTool"}
FILE_WRITE_TOOLS = {"FileWriteTool", "FileEditTool"}

# 文件工具里代表路径的参数名（按序取第一个存在的）
_PATH_ARGS = ("file_path", "path", "pattern_path")


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionDecision:
    decision: Decision
    # 拦截/审批来源：system | project | mode
    source: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def deny_message(self) -> str:
        """拦截提示文案（区分来源，对齐文档 §6）"""
        if self.source == "system":
            return f"该操作被系统内置安全规则禁止: {self.reason}"
        if self.source == "project":
            return f"该操作被本项目 {SECURITY_CONFIG_FILE} 自定义规则禁止: {self.reason}"
        return f"该操作被当前运行模式禁止: {self.reason}"


@dataclass
class ProjectSecurityConfig:
    """项目自定义安全规则（.agent-security.json）"""
    deny_file_patterns: list[str] = field(default_factory=list)
    deny_bash_regex: list[str] = field(default_factory=list)
    source_path: Optional[Path] = None

    @classmethod
    def load(cls, workspace: Path) -> "ProjectSecurityConfig":
        """加载项目配置；不存在=空规则；非法 JSON 丢弃整套并告警不崩溃"""
        path = workspace / SECURITY_CONFIG_FILE
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[permission] %s 解析失败，丢弃自定义规则回退系统默认: %s", path, exc)
            return cls()
        if not isinstance(data, dict):
            logger.warning("[permission] %s 格式非法（非对象），丢弃自定义规则", path)
            return cls()

        config = cls(source_path=path)
        # 仅认两个固定 Key，未知 Key 全部忽略不报错（文档 §5.3）
        patterns = data.get("denyFilePatterns")
        if isinstance(patterns, list):
            config.deny_file_patterns = [str(p) for p in patterns if isinstance(p, str)]
        regexes = data.get("denyBashRegex")
        if isinstance(regexes, list):
            valid = []
            for r in regexes:
                if not isinstance(r, str):
                    continue
                try:
                    re.compile(r)
                    valid.append(r)
                except re.error as exc:
                    logger.warning("[permission] 无效正则已忽略: %r (%s)", r, exc)
            config.deny_bash_regex = valid
        if config.deny_file_patterns or config.deny_bash_regex:
            logger.info(
                "[permission] 项目规则加载: %d file patterns, %d bash regexes",
                len(config.deny_file_patterns), len(config.deny_bash_regex),
            )
        return config

    # ----------------------------------------------------------
    # 匹配
    # ----------------------------------------------------------

    def match_file(self, rel_path: str) -> Optional[str]:
        """相对路径（posix）命中 denyFilePatterns → 返回命中的 pattern

        glob 语义约定（从严：宁可多拦）：
        - "**/.env"          → 任意深度的 .env（含根下）
        - "**/config/secret/**" → 该目录下的一切（含任意深度定位）
        - "src/*.py"         → 字面 glob（fnmatch 的 * 可跨段，从严）
        """
        from fnmatch import fnmatch

        normalized = rel_path.replace("\\", "/").lstrip("./")
        if not normalized:
            return None
        filename = normalized.rsplit("/", 1)[-1]

        for pattern in self.deny_file_patterns:
            p = pattern.replace("\\", "/").strip("/")
            if not p:
                continue
            core = p[3:] if p.startswith("**/") else p

            # 整条 glob 匹配（** 前缀等价于任意深度前缀）
            if fnmatch(normalized, p) or fnmatch(normalized, f"**/{p}"):
                return pattern
            # core 按文件名 / 路径后缀匹配（任意深度）
            if fnmatch(filename, core) or normalized == core or normalized.endswith("/" + core):
                return pattern
            # 目录前缀：xxx/** → 该目录下一切
            if core.endswith("/**"):
                prefix = core[:-3].rstrip("/")
                if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
                    return pattern
        return None

    def match_bash(self, command: str) -> Optional[str]:
        """命令命中 denyBashRegex → 返回命中的正则"""
        for pattern in self.deny_bash_regex:
            try:
                if re.search(pattern, command):
                    return pattern
            except re.error:
                continue
        return None


class PermissionManager:
    """全局权限管理器（单例，跟随 AgentHost 生命周期）

    所有工具调用必经 check()；三级拦截顺序不可改动。
    """

    def __init__(self) -> None:
        self._project = ProjectSecurityConfig()
        self._workspace: Optional[Path] = None
        self._approval_handler: Optional[Callable[[dict[str, Any]], bool]] = None
        # ASK 审批串行锁（并发多会话时一次只处理一个审批，避免 input() 交错）
        self._ask_lock = threading.Lock()

    # ============================================================
    # 初始化
    # ============================================================

    def load_project(self, workspace: Path) -> None:
        """项目打开时加载一次（不热更，重启生效）"""
        self._workspace = Path(workspace)
        self._project = ProjectSecurityConfig.load(self._workspace)

    def set_approval_handler(self, handler: Callable[[dict[str, Any]], bool] | None) -> None:
        """设置 ASK 审批回调（控制台 Y/N）；None = 无交互，ASK 自动降级 DENY"""
        self._approval_handler = handler

    # ============================================================
    # 统一校验入口
    # ============================================================

    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_mode: str = "auto",
        approved: bool = False,
    ) -> PermissionDecision:
        """三级权限校验（approved=True 表示人工已确认，仅跳过模式层）"""
        span = self._start_span(tool_name, args, agent_mode)
        try:
            decision = self._check_inner(tool_name, args, agent_mode, approved)
        except Exception as exc:
            logger.warning("[permission] check error: %s", exc, exc_info=True)
            decision = PermissionDecision(Decision.ALLOW, "system", "check-internal-error-pass")
        self._finish_span(span, decision)
        return decision

    def _check_inner(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_mode: str,
        approved: bool,
    ) -> PermissionDecision:
        # MCP / Skill / 元工具：本期不做权限校验（用户决策）
        if tool_name.startswith(("mcp_", "skill_")) or tool_name in ("mcp_search", "skill_search"):
            return PermissionDecision(Decision.ALLOW, "mode", "mcp/skill not gated this phase")

        ws = self._workspace or Path.cwd()

        # ---- 1. 系统内置黑名单（最高优先级，人工确认也不可放行）----
        if tool_name == "BashTool":
            from ..tools.bash_tool import _looks_dangerous  # 复用危险命令正则

            command = str(args.get("command", ""))
            hit = _looks_dangerous(command)
            if hit:
                return PermissionDecision(Decision.DENY, "system", f"dangerous command pattern: {hit}")

        if tool_name in FILE_TOOLS:
            path_str = self._extract_path(args)
            if path_str:
                try:
                    candidate = Path(path_str).expanduser()
                    if not candidate.is_absolute():
                        candidate = ws / candidate  # 相对路径锚定到 workspace 再校验
                    fake = WorkspaceNs(ws)
                    validate_path_access(fake, candidate, "write" if tool_name in FILE_WRITE_TOOLS else "read")
                except PathSecurityError as exc:
                    return PermissionDecision(Decision.DENY, "system", str(exc))
                except Exception as exc:
                    logger.debug("[permission] path validation skipped: %s", exc)

        # ---- 2. 项目自定义黑名单 ----
        if tool_name == "BashTool":
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

        # ---- 3. 会话模式规则 ----
        mode = (agent_mode or "auto").lower()
        if approved:
            # 人工已确认（单次生效）：仅跳过模式层，黑名单已在上面全量校验
            return PermissionDecision(Decision.ALLOW, "mode", f"approved-once ({mode})")

        if mode == "plan":
            if tool_name == "BashTool":
                return PermissionDecision(Decision.DENY, "mode", "plan mode: all bash commands denied")
            if tool_name in FILE_WRITE_TOOLS:
                return PermissionDecision(Decision.ASK, "mode", "plan mode: file write needs approval")
            return PermissionDecision(Decision.ALLOW, "mode", "plan mode read-only")

        if mode == "edit":
            if tool_name == "BashTool":
                return PermissionDecision(Decision.ASK, "mode", "edit mode: bash needs approval")
            return PermissionDecision(Decision.ALLOW, "mode", "edit mode file free")

        # auto：全部放行（黑名单已拦截）
        return PermissionDecision(Decision.ALLOW, "mode", "auto mode")

    # ============================================================
    # ASK 审批（控制台 Y/N；无交互自动降级 DENY）
    # ============================================================

    def ask_user(self, tool_name: str, args: dict[str, Any], decision: PermissionDecision,
                 *, agent_mode: str = "") -> bool:
        """发起人工审批；返回 True=确认（单次生效）。

        全局串行：并发多会话同时 ASK 时一次只处理一个（CLI 单 stdin / 单用户），
        避免提示交错。持锁者等用户输入（不等 waiter 资源，无死锁）。
        """
        info = {
            "tool_name": tool_name,
            "agent_mode": agent_mode,
            "source": decision.source,
            "reason": decision.reason,
            "args": dict(args),
        }
        with self._ask_lock:
            handler = self._approval_handler
            if handler is None:
                logger.info("[permission] ASK 无审批入口（无头环境），自动拦截: %s %s", tool_name, decision.reason)
                return False
            try:
                return bool(handler(info))
            except (EOFError, KeyboardInterrupt):
                logger.info("[permission] 审批输入不可用，自动拦截: %s", tool_name)
                return False
            except Exception as exc:
                logger.warning("[permission] 审批回调异常，自动拦截: %s", exc)
                return False

    # ============================================================
    # Internal
    # ============================================================

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

    def _start_span(self, tool_name: str, args: dict[str, Any], agent_mode: str) -> Any:
        try:
            from ..trace import get_tracer

            span = get_tracer().start_span(
                "permission", "permission_check",
                tags={"tool_name": tool_name, "agent_mode": agent_mode},
                input_summary=str(args)[:120],
            )
            span._begin()
            return span
        except Exception:
            return None

    def _finish_span(self, span: Any, decision: PermissionDecision) -> None:
        if span is None:
            return
        try:
            span.set_tag("decision", decision.decision.value)
            span.set_tag("source", decision.source)
            span.set_output_summary(decision.reason or decision.decision.value)
            span.finish()
        except Exception as exc:
            logger.debug("[permission] trace span finish failed: %s", exc)


# ============================================================
# 全局单例
# ============================================================

_manager: Optional[PermissionManager] = None


def get_permission_manager() -> PermissionManager:
    global _manager
    if _manager is None:
        _manager = PermissionManager()
    return _manager


def reset_permission_manager() -> None:
    """重置单例（测试用）"""
    global _manager
    _manager = None


def make_console_approval_handler() -> Callable[[dict[str, Any]], bool]:
    """控制台 Y/N 审批（本地调试场景，对齐文档 §10.4.2 修正版）"""
    def handler(info: dict[str, Any]) -> bool:
        print()
        print("【权限审批需人工确认】")
        print(f"  运行模式: {info.get('agent_mode', '')}")
        print(f"  待执行工具: {info.get('tool_name', '')}")
        print(f"  规则来源: {info.get('source', '')} ({info.get('reason', '')})")
        print(f"  参数: {str(info.get('args', {}))[:200]}")
        answer = input("  风险操作需手动确认，输入 Y 执行 / N 取消: ").strip().lower()
        return answer in ("y", "yes")
    return handler
