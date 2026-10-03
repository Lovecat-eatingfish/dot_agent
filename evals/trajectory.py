"""
evals.trajectory — 从 agent 会话 JSONL 抽取轨迹数据（供指标层消费）

数据源：agent run 产生的 .dot/sessions/<id>/session.jsonl
口径（v1，近似）：
  turns      = 不同 turn_id 数
  llm_calls  ≈ assistant 消息条数（每次 API 回合一条）
  tool_calls = toolCall 条数
  tokens     = 会话若记录 usage 则累加，否则 None
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _load_messages(session_file: Path) -> list[dict]:
    messages = []
    for line in session_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "msg":
            # turn_id 在记录层，并入 message 便于统一遍历
            message = dict(record.get("message") or {})
            message["turn_id"] = record.get("turn_id")
            messages.append(message)
    return messages


def collect_metrics(session_file: Path) -> dict:
    """解析单个 session JSONL，返回指标 dict"""
    messages = _load_messages(session_file)
    turns = set()
    tool_calls = 0
    tool_names: dict[str, int] = {}
    assistant_msgs = 0
    tokens = 0
    has_usage = False
    has_final_text = False
    suspicious: list[str] = []

    workspace = str(Path(session_file).resolve().parents[3])  # ws/.dot/sessions/x.jsonl → ws

    # usage 记录（前向兼容：session 落盘若含 {"type":"usage"} 则累加真实 token）
    for line in session_file.read_text(encoding="utf-8").splitlines():
        if '"usage"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "usage":
            continue
        has_usage = True
        tokens += int(record.get("total_tokens", 0) or 0)

    def _flag_outside(path_str: str, kind: str) -> None:
        p = Path(path_str)
        if not p.is_absolute():
            p = (Path(workspace) / p)
        try:
            outside = not p.resolve().is_relative_to(Path(workspace).resolve())
        except (OSError, ValueError):
            outside = True
        if outside:
            suspicious.append(f"{kind}:{path_str}")

    for m in messages:
        if "turn_id" in m:
            turns.add(m["turn_id"])
        role = m.get("role")
        content = m.get("content")
        items = content if isinstance(content, list) else []
        if role == "assistant":
            assistant_msgs += 1
            # 最终可见回答是否存在（限流中断的典型特征：只有工具调用、无结论文本）
            for item in items:
                if isinstance(item, dict) and item.get("type") == "text" \
                        and str(item.get("text", "")).strip():
                    has_final_text = True
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "toolCall":
                tool_calls += 1
                name = item.get("name", "?")
                tool_names[name] = tool_names.get(name, 0) + 1
                args = item.get("arguments") or {}
                if name in ("write_file", "edit_file", "read_file"):
                    _flag_outside(str(args.get("file_path", "")), name)
                elif name == "bash":
                    cmd = str(args.get("command", ""))
                    # 只标记路径形态的 ".."（../ 或 ..\ 或独立 .. 记号），
                    # 避免误伤 findstr/regex 里的连续点号（如 "......$"）
                    if re.search(r"\.\.[/\\]|(?:^|\s)\.\.(?=\s|$)", cmd):
                        suspicious.append(f"bash-..:{cmd[:80]}")
            if item.get("type") == "usage" or "usage" in item:
                has_usage = True
                usage = item.get("usage") or {}
                tokens += int(usage.get("total_tokens", 0) or 0)

    return {
        "turns": len(turns),
        "llm_calls": assistant_msgs,
        "tool_calls": tool_calls,
        "tool_names": tool_names,
        "tokens": tokens if has_usage else None,
        "has_final_text": has_final_text,
        "security": {"out_of_workspace_args": suspicious},
    }
