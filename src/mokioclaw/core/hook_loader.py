"""
从配置文件加载 Hook

支持两级配置合并：
1. 用户全局：~/.mokioclaw/hooks.json
2. 项目级：.mokioclaw/hooks.json

格式（对齐 Claude Code settings.json hooks 段）：
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^Bash",
        "hooks": [
          {"type": "command", "command": ".mokioclaw/hooks/shell-check.sh"}
        ]
      }
    ]
  }
}

当前实现支持三种 type（对齐 Claude Code）：
- command: 外部脚本，stdin 收 JSON，exit 2 阻断
- http:   POST JSON 到 URL，HTTP 200 放行 / 4xx 阻断 / 5xx 告警
- prompt: LLM 评审，返回 JSON 决策（allow/deny/ask + 上下文注入）
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mokioclaw.core.hooks import Hook, HookEvent, HookPayload, HookResult, HookRunner
from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import json_safe

logger = get_logger(__name__)

_GLOBAL_HOOKS = Path.home() / ".mokioclaw" / "hooks.json"
_PROJECT_HOOKS = Path(".mokioclaw") / "hooks.json"
_DEFAULT_TIMEOUT = 30


# 给HookRunner 执行引擎 注册所有的hook
def load_hooks_into_runner(
    runner: HookRunner,
    workspace: Path | None = None,
) -> int:
    """加载配置中的 Hook 到 runner，返回注册数量"""
    configs = _load_merged_configs(workspace)
    count = 0
    for event_name, entries in configs.items():
        try:
            event = HookEvent(event_name)
        except ValueError:
            logger.warning("Unknown hook event '%s', skipped", event_name)
            continue
        for entry in entries:
            matcher = str(entry.get("matcher", "") or "")
            for idx, hook_def in enumerate(entry.get("hooks") or []):
                if not isinstance(hook_def, dict):
                    continue
                hook_type = str(hook_def.get("type", "command")).lower()
                handler = _build_handler(hook_type, hook_def, workspace)
                if handler is None:
                    logger.debug("Unsupported hook type '%s', skipped", hook_type)
                    continue
                name = f"cfg:{event_name}:{matcher or '*'}:{idx}"
                runner.register(
                    Hook(
                        name=name,
                        events=(event,),
                        matcher=matcher,
                        handler=handler,
                        priority=int(hook_def.get("priority", 100) or 100),
                    )
                )
                count += 1
    if count:
        logger.info("Loaded %d hooks from config", count)
    return count


def _load_merged_configs(workspace: Path | None) -> dict[str, list[dict[str, Any]]]:
    """合并全局 + 项目 + 已启用插件 hooks 配置"""
    merged: dict[str, list[dict[str, Any]]] = {}
    for path in (_GLOBAL_HOOKS, _project_hooks_path(workspace)):
        data = _read_hooks_file(path)
        if not data:
            continue
        hooks_block = data.get("hooks", data)
        if not isinstance(hooks_block, dict):
            continue
        for event_name, entries in hooks_block.items():
            if not isinstance(entries, list):
                continue
            merged.setdefault(str(event_name), []).extend(
                e for e in entries if isinstance(e, dict)
            )
    try:
        from mokioclaw.plugins.loader import merge_plugin_hooks_configs

        for event_name, entries in merge_plugin_hooks_configs(workspace).items():
            merged.setdefault(event_name, []).extend(entries)
    except Exception as exc:
        logger.debug("plugin hooks merge skipped: %s", exc)
    return merged


def _project_hooks_path(workspace: Path | None) -> Path:
    if workspace is None:
        return Path.cwd() / _PROJECT_HOOKS
    return workspace / ".mokioclaw" / "hooks.json"


def _read_hooks_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load hooks from %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _build_handler(
    hook_type: str,
    hook_def: dict[str, Any],
    workspace: Path | None,
):
    """根据 type 构建对应的 hook handler，返回 None 表示不支持的类型"""
    if hook_type == "command":
        command = str(hook_def.get("command", "")).strip()
        if not command:
            return None
        timeout = int(hook_def.get("timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
        return _make_command_handler(command, workspace, timeout)
    if hook_type == "http":
        url = str(hook_def.get("url", "")).strip()
        if not url:
            return None
        method = str(hook_def.get("method", "POST")).upper()
        headers = hook_def.get("headers") or {}
        timeout = int(hook_def.get("timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
        return _make_http_handler(url, method, headers, timeout)
    if hook_type == "prompt":
        prompt_template = str(hook_def.get("prompt", "")).strip()
        if not prompt_template:
            return None
        return _make_prompt_handler(prompt_template)
    return None


def _make_command_handler(
    command: str,
    workspace: Path | None,
    timeout: int,
):
    """创建 command 类型 Hook handler

    契约（对齐 Claude Code）：
    - exit 0: 放行；stdout JSON 可含 updated_args
    - exit 2: 阻断；stderr 作为 feedback
    - 其他: 告警，不阻断
    """
    cwd = str(workspace) if workspace else os.getcwd()

    def handler(payload: HookPayload) -> HookResult:
        stdin_data = json.dumps(
            json_safe({
                "hook_event_name": payload.event.value,
                "tool_name": payload.tool_name,
                "tool_input": payload.tool_args,
                "session_id": payload.session_id,
                "cwd": payload.workspace or cwd,
                "user_prompt": payload.user_prompt,
            }),
            ensure_ascii=False,
        )
        try:
            completed = subprocess.run(
                command,
                input=stdin_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=True,
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Hook command timed out: %s", command)
            return HookResult()
        except Exception as exc:
            logger.warning("Hook command failed: %s (%s)", command, exc)
            return HookResult()

        if completed.returncode == 2:
            feedback = (completed.stderr or completed.stdout or "blocked by hook").strip()
            return HookResult(blocked=True, feedback=feedback)

        if completed.returncode != 0:
            logger.warning(
                "Hook command exit %s: %s — %s",
                completed.returncode,
                command,
                (completed.stderr or "").strip()[:200],
            )
            return HookResult()

        stdout = (completed.stdout or "").strip()
        return _parse_hook_json_response(stdout, payload)

    return handler


def _make_http_handler(
    url: str,
    method: str,
    headers: dict[str, Any],
    timeout: int,
):
    """创建 http 类型 Hook handler

    契约（对齐 Claude Code command 契约的 HTTP 等价）：
    - POST/GET JSON payload 到 url
    - HTTP 200: 放行；响应体 JSON 可含 updated_args / permissionDecision / additionalContext
    - HTTP 4xx: 阻断（对齐 exit 2）；响应体作为 feedback
    - HTTP 5xx / 超时: 告警，不阻断（对齐 command 超时行为）
    """
    def handler(payload: HookPayload) -> HookResult:
        body = json.dumps(
            json_safe({
                "hook_event_name": payload.event.value,
                "tool_name": payload.tool_name,
                "tool_input": payload.tool_args,
                "session_id": payload.session_id,
                "user_prompt": payload.user_prompt,
            }),
            ensure_ascii=False,
        ).encode("utf-8")
        req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if isinstance(headers, dict):
            req_headers.update({str(k): str(v) for k, v in headers.items()})
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # urlopen 对 4xx 和 5xx 一律抛 HTTPError（HTTPError 是 URLError 子类）。
            # 按状态码区分，对齐契约：4xx 阻断、5xx 告警不阻断。
            if 400 <= exc.code < 500:
                try:
                    feedback = exc.read().decode("utf-8", errors="replace").strip()
                except Exception:  # noqa: BLE001
                    feedback = ""
                return HookResult(blocked=True, feedback=feedback or f"hook http {exc.code}")
            logger.warning("Hook http server error %s: %s (%s)", exc.code, url, exc)
            return HookResult()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 连接错误 / 超时 → 告警不阻断（对齐 command 超时）
            logger.warning("Hook http failed: %s (%s)", url, exc)
            return HookResult()

        try:
            raw = resp.read().decode("utf-8", errors="replace").strip()
        finally:
            resp.close()

        # 解析响应 JSON → HookResult（复用 command handler 的字段映射逻辑）
        return _parse_hook_json_response(raw, payload)

    return handler


def _make_prompt_handler(prompt_template: str):
    """创建 prompt 类型 Hook handler

    契约（对齐 Claude Code prompt hook）：
    - 用 LLM 评审 payload，输出 JSON 决策
    - 决策 allow/deny/ask + 可选 additionalContext
    - LLM 不可用 / 返回非 JSON → 不阻断（fail-open，对齐 Claude Code）

    复用 security/classifier.py 的 LLM 调用 + JSON 解析模式。
    """
    def handler(payload: HookPayload) -> HookResult:
        # 填充 prompt 模板：替换 {tool_name} / {tool_input} / {user_prompt} 等占位
        prompt = prompt_template.format(
            tool_name=payload.tool_name or "",
            tool_input=json.dumps(json_safe(payload.tool_args), ensure_ascii=False),
            user_prompt=payload.user_prompt or "",
            session_id=payload.session_id or "",
            event=payload.event.value,
        )
        try:
            from mokioclaw.providers.openai_provider import create_model

            response = create_model().invoke(prompt)
            raw = getattr(response, "content", "") or str(response)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hook prompt LLM failed: %s", exc)
            return HookResult()

        return _parse_hook_json_response(str(raw), payload)

    return handler


def _parse_hook_json_response(raw: str, payload: HookPayload) -> HookResult:
    """解析 hook 返回的 JSON 文本为 HookResult（command/http/prompt 共用）

    支持 Claude Code 字段：permissionDecision / additionalContext /
    updatedInput / preventContinuation / decision。
    """
    blocked = False
    feedback = ""
    updated_args = None
    context_injection = ""
    permission_decision = ""
    prevent_continuation = False

    raw = (raw or "").strip()
    if not raw:
        return HookResult()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 纯文本 → 上下文注入（SessionStart 等）
        if payload.event in (
            HookEvent.SessionStart,
            HookEvent.UserPromptSubmit,
            HookEvent.Stop,
            HookEvent.SubagentStop,
        ):
            context_injection = raw
        return HookResult(context_injection=context_injection)

    if isinstance(parsed, dict):
        if isinstance(parsed.get("updated_args"), dict) or isinstance(parsed.get("updatedInput"), dict):
            updated_args = parsed.get("updated_args") or parsed.get("updatedInput")
        for key in ("additional_context", "additionalContext", "systemMessage"):
            if isinstance(parsed.get(key), str) and parsed[key].strip():
                context_injection = parsed[key].strip()
                break
        specific = parsed.get("hookSpecificOutput")
        if isinstance(specific, dict):
            decision = str(specific.get("permissionDecision") or "").lower()
            if decision in {"allow", "deny", "ask"}:
                permission_decision = decision
            reason = specific.get("permissionDecisionReason") or specific.get("reason")
            if isinstance(reason, str) and reason.strip():
                feedback = reason.strip()
            if isinstance(specific.get("updatedInput"), dict):
                updated_args = specific["updatedInput"]
            if isinstance(specific.get("additionalContext"), str):
                context_injection = specific["additionalContext"]
        if not permission_decision:
            decision = str(parsed.get("permissionDecision") or "").lower()
            if decision in {"allow", "deny", "ask"}:
                permission_decision = decision
        if not feedback and isinstance(parsed.get("reason"), str):
            feedback = parsed["reason"].strip()
        decision_top = str(parsed.get("decision") or "").lower()
        if (
            parsed.get("preventContinuation") is True
            or decision_top == "block"
            or (
                payload.event in (HookEvent.Stop, HookEvent.SubagentStop)
                and parsed.get("continue") is True
            )
        ):
            prevent_continuation = True
            if not feedback and isinstance(parsed.get("reason"), str):
                feedback = parsed["reason"]
    blocked = permission_decision == "deny"
    return HookResult(
        blocked=blocked,
        feedback=feedback,
        updated_args=updated_args,
        context_injection=context_injection,
        permission_decision=permission_decision,
        prevent_continuation=prevent_continuation,
    )
