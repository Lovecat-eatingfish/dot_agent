"""
contextModifier —— 工具执行后修改后续工具上下文（对齐 Claude Code ToolResult.contextModifier）

约定：工具结果 dict 可含 `_context_modifier`：
  {"cwd": "relative/or/absolute/path"}  → 更新 runtime.cwd（须在 workspace 内）
  {"env": {"KEY": "value"}}           → 合并进 runtime.extra_env
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)


def apply_context_modifier(runtime: Any, result: Any) -> None:
    """从工具结果中读取并应用 context modifier（原地修改 runtime）"""
    if runtime is None or not isinstance(result, dict):
        return
    modifier = result.get("_context_modifier")
    if not isinstance(modifier, dict) or not modifier:
        return

    workspace = getattr(runtime, "workspace", None)
    if workspace is None:
        return
    workspace = Path(workspace).resolve()

    cwd = modifier.get("cwd")
    if cwd:
        try:
            candidate = Path(str(cwd)).expanduser()
            if not candidate.is_absolute():
                base = Path(getattr(runtime, "cwd", None) or workspace)
                candidate = (base / candidate).resolve()
            else:
                candidate = candidate.resolve()
            if _is_under(candidate, workspace) and candidate.is_dir():
                runtime.cwd = candidate
                logger.info("contextModifier: cwd -> %s", candidate)
            else:
                logger.warning("contextModifier ignored cwd outside workspace: %s", candidate)
        except OSError as exc:
            logger.warning("contextModifier cwd failed: %s", exc)

    env = modifier.get("env")
    if isinstance(env, dict):
        extra = getattr(runtime, "extra_env", None)
        if not isinstance(extra, dict):
            extra = {}
            runtime.extra_env = extra
        for key, value in env.items():
            if isinstance(key, str) and key.isidentifier():
                extra[key] = str(value)


def extract_cd_modifier(command: str, runtime: Any) -> dict[str, Any] | None:
    """若命令是纯 cd，返回 context modifier；否则 None"""
    import re

    text = (command or "").strip()
    match = re.fullmatch(r"cd\s+(.+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    target = match.group(1).strip().strip("\"'")
    if not target or target in {".", ""}:
        return None
    workspace = Path(getattr(runtime, "workspace", "."))
    base = Path(getattr(runtime, "cwd", None) or workspace)
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if _is_under(candidate, workspace.resolve()) and candidate.is_dir():
        return {"cwd": str(candidate)}
    return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
