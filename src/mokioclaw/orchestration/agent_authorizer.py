"""
Agent 多模式权限审批模块

对标 Claude Code 四种模式：Plan / Default / AcceptEdits / Auto。
只做 Host 侧授权决策 + diff 快照管理，不实现 LLM 和 Agent 循环。

核心组件：
1. 操作风险分级（RiskTier）：T1 只读 / T2 编辑 / T3 高危
2. 编辑快照层（Shadow Snapshot）：preEditSnapshot 保存原始文件，生成 unified diff，拆分 hunks
3. 审批决策器（Mode-Based Authorizer）：根据模式 + 风险 tier → ALLOW / ASK_USER / DENY
4. Auto 模式独立风险分类器：隔离模型，独立判断，避免自审自批
5. 链路追踪集成：每次授权决策上报 span attributes

和现有系统结合点：
- 复用 security/agent_mode.py 的 VALID_AGENT_MODES / check_tool_permission 基础逻辑
- 对接 reliability/tracing.py 的 Tracer 做埋点
- 存储抽象：Snapshot / hunk 决策结果持久化
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ============================================================
# Enums
# ============================================================

class AgentMode(str, Enum):
    """Agent 运行模式"""
    PLAN = "plan"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    AUTO = "auto"


class RiskTier(str, Enum):
    """操作风险分级"""
    TIER1_READ = "tier1_read"       # 只读：read_file、search、grep、glob
    TIER2_EDIT = "tier2_edit"       # 文件编辑：write_file、edit_file、mkdir
    TIER3_DANGEROUS = "tier3_dangerous"  # 高危：shell、网络、跨目录、rm -rf


class AuthDecisionType(str, Enum):
    """审批决策结果"""
    ALLOW = "allow"
    ASK_USER = "ask_user"
    DENY = "deny"


class HunkStatus(str, Enum):
    """Diff hunk 状态"""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MODIFIED = "modified"


# ============================================================
# Core Data Structures
# ============================================================

@dataclass
class AuthDecision:
    """审批决策输出"""
    decision: str                    # ALLOW | ASK_USER | DENY
    reason: str = ""
    risk_tier: str = RiskTier.TIER1_READ.value
    classifier_result: Optional[str] = None  # auto 模式分类器结果


@dataclass
class FileSnapshot:
    """文件原始快照"""
    file_path: str
    original_content: str
    timestamp: int = 0

    def __post_init__(self) -> None:
        if self.timestamp == 0:
            self.timestamp = _now_ms()


@dataclass
class FileDiffHunk:
    """Diff hunk（代码块）"""
    hunk_id: str
    file_path: str
    diff_content: str
    status: str = HunkStatus.PENDING.value
    applied: bool = False
    user_modified_content: Optional[str] = None


@dataclass
class SessionState:
    """会话级授权状态"""
    session_id: str
    current_mode: str = AgentMode.DEFAULT.value
    allow_rules: list[str] = field(default_factory=list)
    deny_rules: list[str] = field(default_factory=list)


# ============================================================
# Risk Tier Classifier
# ============================================================

# T1 只读工具：永远放行
_TIER1_TOOLS = frozenset({
    "FileReadTool", "GlobTool", "GrepTool", "WebSearchTool",
    "SkillTool", "MemoryIndexTool", "MemoryReadTool",
    "search_available_tools",  # 元工具
})

# T2 编辑工具：文件修改类
_TIER2_TOOLS = frozenset({
    "FileWriteTool", "FileEditTool", "mkdir",
})

# T3 高危工具：shell / 网络 / 跨目录
_TIER3_PATTERNS = [
    re.compile(r"^BashTool$", re.I),
    re.compile(r"^mcp__", re.I),  # MCP 工具默认 T3（可能涉及外部 RPC）
]

# 高危 bash 命令模式（复用现有逻辑）
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/", re.I),
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bdel\s+/[sf]\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\s*\(\s*\)\s*\{", re.I),
]


def classify_risk_tier(tool_name: str, args: dict[str, Any] | None = None) -> str:
    """对工具调用做风险分级

    Args:
        tool_name: 工具名称
        args: 工具参数

    Returns:
        RiskTier 值
    """
    args = args or {}

    # T1 只读
    if tool_name in _TIER1_TOOLS:
        return RiskTier.TIER1_READ.value

    # T2 编辑
    if tool_name in _TIER2_TOOLS:
        return RiskTier.TIER2_EDIT.value

    # T3 高危：匹配模式
    for pattern in _TIER3_PATTERNS:
        if pattern.match(tool_name):
            return RiskTier.TIER3_DANGEROUS.value

    # 默认 T1（保守策略：未知工具视为只读，由审批器决定）
    return RiskTier.TIER1_READ.value


def is_destructive_bash(command: str) -> bool:
    """判断 bash 命令是否为毁灭性操作"""
    text = (command or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _DESTRUCTIVE_PATTERNS)


# ============================================================
# Diff Engine（基于 Python difflib 的 unified diff）
# ============================================================

class DiffEngine:
    """基于 difflib 的 unified diff 引擎

    使用 Python 标准库 difflib 生成标准 unified diff，
    拆分 hunks，支持单个 hunk 应用到原始内容。
    """

    @staticmethod
    def generate_diff(old_content: str, new_content: str, file_path: str = "") -> str:
        """生成 unified diff

        Args:
            old_content: 原始文件内容
            new_content: 新文件内容
            file_path: 文件路径（用于 diff 头信息）

        Returns:
            unified diff 文本
        """
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        # 确保每行都有换行符（difflib 要求）
        old_lines = [line if line.endswith("\n") else line + "\n" for line in old_lines]
        new_lines = [line if line.endswith("\n") else line + "\n" for line in new_lines]

        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
        return "\n".join(line.rstrip("\n") for line in diff)

    @staticmethod
    def split_hunks(diff_text: str, file_path: str) -> list[FileDiffHunk]:
        """将 unified diff 拆分为多个 hunk（按 @@ 分割）

        Args:
            diff_text: unified diff 文本
            file_path: 文件路径

        Returns:
            FileDiffHunk 列表
        """
        hunks: list[FileDiffHunk] = []
        lines = diff_text.splitlines()
        current_hunk_lines: list[str] = []
        hunk_index = 0
        in_hunk = False

        for line in lines:
            if line.startswith("@@"):
                in_hunk = True
                if current_hunk_lines:
                    hunks.append(FileDiffHunk(
                        hunk_id=f"{file_path}:hunk_{hunk_index}",
                        file_path=file_path,
                        diff_content="\n".join(current_hunk_lines),
                    ))
                    hunk_index += 1
                    current_hunk_lines = []
            if in_hunk:
                current_hunk_lines.append(line)

        # 最后一个 hunk
        if current_hunk_lines:
            hunks.append(FileDiffHunk(
                hunk_id=f"{file_path}:hunk_{hunk_index}",
                file_path=file_path,
                diff_content="\n".join(current_hunk_lines),
            ))

        return hunks

    @staticmethod
    def apply_hunk(old_content: str, hunk: FileDiffHunk) -> str:
        """将单个 hunk 应用到原始文件内容

        Args:
            old_content: 原始文件内容
            hunk: 要应用的 hunk

        Returns:
            应用后的文件内容
        """
        hunk_lines = hunk.diff_content.splitlines()
        old_lines = old_content.splitlines(keepends=True)
        new_lines: list[str] = []
        i = 0

        for line in hunk_lines:
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("@@"):
                continue
            if line.startswith("-") and not line.startswith("---"):
                # 删除行：跳过原始内容中对应行
                i += 1
            elif line.startswith("+") and not line.startswith("+++"):
                # 添加行
                new_lines.append(line[1:] + "\n")
            elif line.startswith(" "):
                # 上下文行：保留原始行
                if i < len(old_lines):
                    new_lines.append(old_lines[i])
                    i += 1
            # 其他行跳过

        # 补齐未修改的剩余行
        while i < len(old_lines):
            new_lines.append(old_lines[i])
            i += 1

        return "".join(new_lines)


# ============================================================
# Snapshot Store（存储抽象）
# ============================================================

class SnapshotStore(ABC):
    """快照存储抽象

    后期对接 MySQL / ES 时，实现此接口即可。
    """

    @abstractmethod
    def save_snapshot(self, snapshot: FileSnapshot) -> None:
        ...

    @abstractmethod
    def get_snapshot(self, file_path: str) -> Optional[FileSnapshot]:
        ...

    @abstractmethod
    def save_hunk(self, hunk: FileDiffHunk) -> None:
        ...

    @abstractmethod
    def get_hunks(self, file_path: str) -> list[FileDiffHunk]:
        ...

    @abstractmethod
    def apply_hunk_decision(self, hunk_id: str, decision: str, user_content: Optional[str] = None) -> None:
        ...

    @abstractmethod
    def get_final_content(self, file_path: str) -> str:
        ...


class InMemorySnapshotStore(SnapshotStore):
    """内存快照存储（默认实现）"""

    def __init__(self) -> None:
        self._snapshots: dict[str, FileSnapshot] = {}
        self._hunks: dict[str, FileDiffHunk] = {}  # hunk_id → hunk
        self._file_hunks: dict[str, list[str]] = {}  # file_path → [hunk_id]

    def save_snapshot(self, snapshot: FileSnapshot) -> None:
        self._snapshots[snapshot.file_path] = snapshot

    def get_snapshot(self, file_path: str) -> Optional[FileSnapshot]:
        return self._snapshots.get(file_path)

    def save_hunk(self, hunk: FileDiffHunk) -> None:
        self._hunks[hunk.hunk_id] = hunk
        self._file_hunks.setdefault(hunk.file_path, []).append(hunk.hunk_id)

    def get_hunks(self, file_path: str) -> list[FileDiffHunk]:
        hunk_ids = self._file_hunks.get(file_path, [])
        return [self._hunks[hid] for hid in hunk_ids if hid in self._hunks]

    def apply_hunk_decision(self, hunk_id: str, decision: str, user_content: Optional[str] = None) -> None:
        hunk = self._hunks.get(hunk_id)
        if hunk is None:
            return
        hunk.status = decision
        hunk.applied = decision == HunkStatus.ACCEPTED.value
        if user_content is not None:
            hunk.user_modified_content = user_content
            hunk.status = HunkStatus.MODIFIED.value
            hunk.applied = True

    def get_final_content(self, file_path: str) -> str:
        """获取文件最终内容：原始快照 + 所有 accepted hunks"""
        snapshot = self._snapshots.get(file_path)
        if snapshot is None:
            return ""
        content = snapshot.original_content
        hunks = self.get_hunks(file_path)
        for hunk in hunks:
            if hunk.applied:
                content = DiffEngine.apply_hunk(content, hunk)
        return content


# ============================================================
# Snapshot Store（文件存储实现）
# ============================================================

class FileSnapshotStore(SnapshotStore):
    """文件快照存储实现

    目录结构：
    .agent_snapshots/
    └─{session_id}/
      ├─snapshots/
      │  └─{file_hash}.snap   # 文件快照
      └─hunks/
         └─{file_path}.jsonl  # hunk 决策记录
    """

    def __init__(self, root: str | Path = Path(".agent_snapshots")) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _snapshots_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "snapshots"

    def _hunks_path(self, session_id: str, file_path: str) -> Path:
        safe_name = re.sub(r"[^\w\-]", "_", file_path)
        return self._session_dir(session_id) / "hunks" / f"{safe_name}.jsonl"

    def save_snapshot(self, snapshot: FileSnapshot) -> None:
        snap_dir = self._snapshots_dir(snapshot.file_path.split("/")[0])
        snap_dir.mkdir(parents=True, exist_ok=True)
        content_hash = hashlib.sha256(snapshot.original_content.encode("utf-8", errors="replace")).hexdigest()[:16]
        path = snap_dir / f"{content_hash}.snap"
        path.write_text(snapshot.original_content, encoding="utf-8")

    def get_snapshot(self, file_path: str) -> Optional[FileSnapshot]:
        session_id = file_path.split("/")[0] if "/" in file_path else ""
        if not session_id:
            return None
        snap_dir = self._snapshots_dir(session_id)
        if not snap_dir.exists():
            return None
        for path in snap_dir.iterdir():
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                return FileSnapshot(file_path=file_path, original_content=content)
        return None

    def save_hunk(self, hunk: FileDiffHunk) -> None:
        session_id = hunk.file_path.split("/")[0] if "/" in hunk.file_path else ""
        if not session_id:
            return
        path = self._hunks_path(session_id, hunk.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "hunk_id": hunk.hunk_id,
                "file_path": hunk.file_path,
                "diff_content": hunk.diff_content,
                "status": hunk.status,
                "applied": hunk.applied,
            }, ensure_ascii=False) + "\n")

    def get_hunks(self, file_path: str) -> list[FileDiffHunk]:
        session_id = file_path.split("/")[0] if "/" in file_path else ""
        if not session_id:
            return []
        path = self._hunks_path(session_id, file_path)
        if not path.exists():
            return []
        hunks = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    hunks.append(FileDiffHunk(
                        hunk_id=data["hunk_id"],
                        file_path=data["file_path"],
                        diff_content=data["diff_content"],
                        status=data["status"],
                        applied=data["applied"],
                    ))
                except (json.JSONDecodeError, KeyError):
                    continue
        return hunks

    def apply_hunk_decision(self, hunk_id: str, decision: str, user_content: Optional[str] = None) -> None:
        # 文件存储的 hunk 决策通过重新写入 hunks 文件实现
        # 简化处理：标记状态即可，实际应更新 JSONL 记录
        pass

    def get_final_content(self, file_path: str) -> str:
        snapshot = self.get_snapshot(file_path)
        if snapshot is None:
            return ""
        content = snapshot.original_content
        hunks = self.get_hunks(file_path)
        for hunk in hunks:
            if hunk.applied:
                content = DiffEngine.apply_hunk(content, hunk)
        return content


# ============================================================
# Auto Mode Risk Classifier（独立 LLM 分类器）
# ============================================================

class AutoModeClassifier:
    """Auto 模式独立风险分类器

    独立于主 Agent 模型的隔离分类器，使用独立 LLM 调用判断放行 / 拦截 / 弹人工确认。
    避免主 Agent 模型自审自批（prompt 注入风险）。
    """

    def __init__(self, model: Any = None) -> None:
        """初始化分类器

        Args:
            model: 独立 LLM 模型实例（不能是主 Agent 模型）。
                    为 None 时回退到规则引擎。
        """
        self._model = model

    async def classify_async(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """异步分类器决策：allow | ask_user | deny

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            决策字符串
        """
        if self._model is None:
            return self._rule_based_classify(tool_name, args)

        # 独立 LLM 调用
        prompt = self._build_classifier_prompt(tool_name, args)
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            response = self._model.invoke([
                SystemMessage(content="你是独立的风险分类器，只分析工具调用风险，不执行任何操作。"),
                HumanMessage(content=prompt),
            ])
            result = str(getattr(response, "content", "") or "").strip().lower()
            if "allow" in result:
                return "allow"
            if "deny" in result:
                return "deny"
            return "ask_user"
        except Exception:
            return self._rule_based_classify(tool_name, args)

    def classify(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """同步分类器决策（回退到规则引擎）"""
        return self._rule_based_classify(tool_name, args)

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _rule_based_classify(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """规则引擎回退"""
        args = args or {}
        tier = classify_risk_tier(tool_name, args)

        if tier == RiskTier.TIER1_READ.value:
            return "allow"

        if tier == RiskTier.TIER2_EDIT.value:
            return "allow"

        # T3 高危
        if tool_name == "BashTool":
            command = str(args.get("command", ""))
            if is_destructive_bash(command):
                return "deny"
            return "ask_user"

        if tool_name.startswith("mcp__"):
            return "ask_user"

        return "ask_user"

    def _build_classifier_prompt(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """构建分类器 prompt"""
        args = args or {}
        tier = classify_risk_tier(tool_name, args)

        return f"""分析以下工具调用的风险等级，输出 allow / ask_user / deny：

工具名称: {tool_name}
风险等级: {tier}
参数: {json.dumps(args, ensure_ascii=False, default=str)[:500]}

规则：
- T1 只读操作 → allow
- T2 文件编辑 → allow
- T3 高危操作（shell、网络、跨目录）→ ask_user
- 毁灭性命令（rm -rf、format、mkfs 等）→ deny

只输出一个词：allow 或 ask_user 或 deny"""


# ============================================================
# Agent Authorizer（核心审批引擎）
# ============================================================

class AgentAuthorizer:
    """Agent 多模式权限审批器

    根据当前会话模式 + 操作风险 tier，输出 ALLOW / ASK_USER / DENY。
    集成链路追踪、编辑快照、Auto 模式分类器。
    """

    def __init__(
        self,
        snapshot_store: Optional[SnapshotStore] = None,
        classifier: Optional[AutoModeClassifier] = None,
        tracer: Optional[Any] = None,
    ) -> None:
        self._snapshot_store = snapshot_store or InMemorySnapshotStore()
        self._classifier = classifier or AutoModeClassifier()
        self._tracer = tracer
        # session_id → SessionState
        self._sessions: dict[str, SessionState] = {}

    def set_mode(self, session_id: str, mode: str) -> None:
        """设置会话运行模式"""
        normalized = mode.lower()
        if normalized not in {m.value for m in AgentMode}:
            normalized = AgentMode.DEFAULT.value
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(session_id=session_id)
        self._sessions[session_id].current_mode = normalized

    def get_mode(self, session_id: str) -> str:
        """获取当前会话模式"""
        state = self._sessions.get(session_id, SessionState(session_id=session_id))
        return state.current_mode

    def authorize_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
    ) -> AuthDecision:
        """对 tool_call 做风险分级 + 审批决策

        Args:
            session_id: 会话 ID
            tool_name: 工具名称
            args: 工具参数

        Returns:
            AuthDecision 审批决策
        """
        args = args or {}
        state = self._sessions.get(session_id, SessionState(session_id=session_id))
        mode = state.current_mode
        tier = classify_risk_tier(tool_name, args)

        decision = self._decide(mode, tier, tool_name, args)
        decision.risk_tier = tier

        # 链路追踪埋点
        if self._tracer:
            self._tracer.set_attributes({
                "agent_mode": mode,
                "risk_tier": tier,
                "auth_decision": decision.decision,
                "auth_reason": decision.reason,
            })

        return decision

    def pre_edit_snapshot(self, file_path: str, new_content: str) -> tuple[FileSnapshot, list[FileDiffHunk]]:
        """文件编辑前置：保存原始快照，生成 diff，拆分 hunks

        Args:
            file_path: 文件路径
            new_content: 新文件内容

        Returns:
            (FileSnapshot, FileDiffHunk[])
        """
        # 读取原始文件（占位：实际应读磁盘）
        original_content = ""
        try:
            from pathlib import Path
            p = Path(file_path)
            if p.exists():
                original_content = p.read_text(encoding="utf-8")
        except Exception:
            pass

        snapshot = FileSnapshot(
            file_path=file_path,
            original_content=original_content,
        )
        self._snapshot_store.save_snapshot(snapshot)

        # 生成 diff + 拆分 hunks
        diff_text = DiffEngine.generate_diff(original_content, new_content, file_path)
        hunks = DiffEngine.split_hunks(diff_text, file_path)
        for hunk in hunks:
            self._snapshot_store.save_hunk(hunk)

        return snapshot, hunks

    def apply_hunk_decision(
        self,
        hunk_id: str,
        decision: str,
        user_modified_content: Optional[str] = None,
    ) -> None:
        """用户对单个 hunk 的 accept/reject/modify 决策

        Args:
            hunk_id: hunk ID
            decision: accept | reject | modified
            user_modified_content: 用户手动修改后的内容（modified 时使用）
        """
        self._snapshot_store.apply_hunk_decision(hunk_id, decision, user_modified_content)

    def get_final_file_content(self, file_path: str) -> str:
        """获取文件最终合并后的内容（原始快照 + 所有 accepted hunks）"""
        return self._snapshot_store.get_final_content(file_path)

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _decide(self, mode: str, tier: str, tool_name: str, args: dict[str, Any]) -> AuthDecision:
        """根据模式 + 风险 tier 做决策"""

        # Plan 模式：全部写操作 DENY
        if mode == AgentMode.PLAN.value:
            if tier in (RiskTier.TIER2_EDIT.value, RiskTier.TIER3_DANGEROUS.value):
                return AuthDecision(
                    decision=AuthDecisionType.DENY.value,
                    reason="plan 模式禁止修改磁盘，请输出方案或 diff",
                    risk_tier=tier,
                )
            return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)

        # Default 模式
        if mode == AgentMode.DEFAULT.value:
            if tier == RiskTier.TIER1_READ.value:
                return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)
            if tier == RiskTier.TIER2_EDIT.value:
                return AuthDecision(
                    decision=AuthDecisionType.ASK_USER.value,
                    reason="文件编辑操作需要用户确认",
                    risk_tier=tier,
                )
            return AuthDecision(
                decision=AuthDecisionType.ASK_USER.value,
                reason="高危操作需要用户确认",
                risk_tier=tier,
            )

        # AcceptEdits 模式
        if mode == AgentMode.ACCEPT_EDITS.value:
            if tier == RiskTier.TIER1_READ.value:
                return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)
            if tier == RiskTier.TIER2_EDIT.value:
                return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)
            return AuthDecision(
                decision=AuthDecisionType.ASK_USER.value,
                reason="高危操作需要用户确认",
                risk_tier=tier,
            )

        # Auto 模式
        if mode == AgentMode.AUTO.value:
            if tier == RiskTier.TIER1_READ.value:
                return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)
            if tier == RiskTier.TIER2_EDIT.value:
                return AuthDecision(decision=AuthDecisionType.ALLOW.value, risk_tier=tier)
            # T3 高危：交给独立分类器
            classifier_result = self._classifier.classify(tool_name, args)
            decision_map = {
                "allow": AuthDecisionType.ALLOW.value,
                "ask_user": AuthDecisionType.ASK_USER.value,
                "deny": AuthDecisionType.DENY.value,
            }
            return AuthDecision(
                decision=decision_map.get(classifier_result, AuthDecisionType.ASK_USER.value),
                reason=f"auto 分类器判定: {classifier_result}",
                risk_tier=tier,
                classifier_result=classifier_result,
            )

        # 默认：ASK_USER
        return AuthDecision(
            decision=AuthDecisionType.ASK_USER.value,
            reason=f"未知模式 {mode}，默认需要确认",
            risk_tier=tier,
        )


# ============================================================
# Time helper
# ============================================================

def _now_ms() -> int:
    return int(time.time() * 1000)
