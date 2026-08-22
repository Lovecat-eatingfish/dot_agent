"""
dot agent 控制台调试入口

用法：
    python -m dot                     # 交互模式
    python -m dot "写个 hello world"  # 单次任务

命令（交互模式内）：
    /sessions              列出磁盘上的所有会话
    /resume ID             切换到指定会话
    /turns                 列出当前会话的可回滚轮次
    /rewind N              回滚到第 N 轮（对话 + 用户代码）
    /mode plan|edit|auto   切换工作模式（默认 auto）
    /quit                  退出

工作模式（对齐 doc/fix.md）：
    plan  只有只读工具
    edit  bash 命令每次审批，其余随意执行
    auto  权限最大
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from dot.host.agent_host import AgentHost
from dot.core.log import get_logger, setup_logging

logger = get_logger(__name__)

AGENT_MODES = ("plan", "edit", "auto")

# 审批提示音（Windows winsound）
def _play_approval_sound() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def main() -> None:
    """控制台入口"""
    setup_logging()
    logger.info("dot agent starting, argv=%s", sys.argv)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        raise SystemExit(0)

    workspace = Path(r'D:\桌面\test\ceshi_dotagent')
    host = AgentHost(workspace=workspace)
    session = host.get_or_create_session()
    logger.info("AgentHost initialized, workspace=%s, active session=%s", workspace, session.session_id)

    if args:
        _run_once(host, " ".join(args))
    else:
        _run_interactive(host)


def _run_once(host: AgentHost, task: str) -> None:
    """单次执行任务并打印结果"""
    _run_turn(host, task)


def _run_interactive(host: AgentHost) -> None:
    """交互模式：循环读取用户输入并执行"""
    session = host.get_or_create_session()
    agent_mode = "auto"

    # 跨进程恢复：上轮结束时可能停在人工介入状态（自定义机制，靠 session.json）
    if host.has_pending_intervention():
        print()
        print("[intervene] 检测到未处理的人工介入（上次会话遗留）。")
        _handle_intervention(host, agent_mode=agent_mode)

    print()

    while True:
        try:
            user_input = input(f"[{agent_mode}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[dot] Bye.")
            return

        if not user_input:
            continue

        if user_input == "/quit":
            print("[dot] Bye.")
            return
        if user_input == "/sessions":
            _list_sessions(host)
            continue
        if user_input.startswith("/resume"):
            _resume_session(host, user_input)
            continue
        if user_input == "/turns":
            session = host.get_or_create_session()
            available = host.list_available_turns(session.session_id)
            print(f"[dot] available turns: {available}")
            continue
        if user_input.startswith("/rewind"):
            _rewind(host, user_input)
            continue
        if user_input.startswith("/mode"):
            new_mode = _parse_mode(user_input, agent_mode)
            if new_mode:
                agent_mode = new_mode
                print(f"[dot] agent_mode = {agent_mode}")
            continue

        _run_turn(host, user_input, agent_mode=agent_mode)


def _run_turn(host: AgentHost, user_input: str, *, agent_mode: str = "auto") -> None:
    """执行一轮任务：跑图 → 检查介入状态 → 打印事件"""
    logger.info("=== turn start: input=%r, mode=%s ===", user_input, agent_mode)
    try:
        for chunk in host.run(user_input, agent_mode=agent_mode):
            logger.debug("chunk: %s", _safe_chunk(chunk))
            _handle_chunk(chunk)

        # 自定义介入：本轮已正常结束（finally 已持久化），但停在介入状态
        if host.has_pending_intervention():
            _handle_intervention(host, agent_mode=agent_mode)

        logger.info("=== turn end ===")
    except KeyboardInterrupt:
        print("\n[dot] Interrupted.")
        logger.info("turn interrupted by user")
    except Exception as exc:
        print(f"\n[dot] Error: {exc}")
        logger.error("turn failed: %s", exc, exc_info=True)


def _handle_intervention(host: AgentHost, *, agent_mode: str = "auto") -> None:
    """处理人工介入（自定义机制）：询问 continue / stop"""
    print()
    print("[intervene] 需要人工介入（replan/attempt 已达上限）。")
    while True:
        answer = input("[intervene] continue 重新规划 / stop 结束本轮? [c/s]: ").strip().lower()
        if answer in ("c", "continue"):
            action = "continue"
            break
        if answer in ("s", "stop", ""):
            action = "stop"
            break
        print("请输入 c (continue) 或 s (stop)")

    logger.info("intervention resume action=%s", action)
    try:
        for chunk in host.resume_intervention(action, agent_mode=agent_mode):
            logger.debug("resume chunk: %s", _safe_chunk(chunk))
            _handle_chunk(chunk)
    except KeyboardInterrupt:
        print("\n[dot] Interrupted.")
    except Exception as exc:
        print(f"\n[dot] Error: {exc}")
        logger.error("resume failed: %s", exc, exc_info=True)


def _handle_chunk(chunk: dict[str, Any]) -> None:
    """打印 graph stream chunk 的关键事件（细节走 logger）"""
    for node_name, update in chunk.items():
        if not isinstance(update, dict):
            continue
        if node_name == "finally_node":
            answer = update.get("final_answer", "")
            print(f"\n[final] {answer}")
            logger.info("[final] %s", answer)
        elif node_name == "human_intervene":
            logger.info("[intervene] node executed (turn persisted, awaiting user decision)")


def _parse_mode(user_input: str, current: str) -> str | None:
    parts = user_input.split()
    if len(parts) < 2 or parts[1] not in AGENT_MODES:
        print(f"[dot] Usage: /mode {'|'.join(AGENT_MODES)}  (current: {current})")
        return None
    return parts[1]


def _list_sessions(host: AgentHost) -> None:
    """列出磁盘上的所有会话"""
    sessions = host.list_sessions()
    if not sessions:
        print("[dot] No sessions found.")
        return
    for info in sessions:
        task_str = info["task"] or "(no task)"
        flag = " [awaiting]" if info.get("awaiting_intervention") else ""
        print(f"  {info['session_id']}  turn={info['turn_id']}  msgs={info['messages']}  {task_str}{flag}")


def _resume_session(host: AgentHost, user_input: str) -> None:
    """切换到指定会话"""
    parts = user_input.split()
    if len(parts) < 2:
        print("[dot] Usage: /resume <session_id>")
        return
    sid = parts[1]
    sessions = {s["session_id"] for s in host.list_sessions()}
    if sid not in sessions:
        print(f"[dot] Session {sid} not found. Known: {sorted(sessions)}")
        return
    session = host.get_or_create_session(sid)
    print(f"[dot] Resumed session {session.session_id} (turn {session.current_turn_id}, {len(session.messages)} msgs)")
    if host.has_pending_intervention(sid):
        print("[intervene] 该会话有未处理的人工介入。")
        _handle_intervention(host)


def _rewind(host: AgentHost, user_input: str) -> None:
    """回滚到指定 turn"""
    parts = user_input.split()
    if len(parts) < 2:
        print("[dot] Usage: /rewind N")
        return
    try:
        turn = int(parts[1])
    except ValueError:
        print("[dot] Usage: /rewind N")
        return
    session = host.get_or_create_session()
    available = host.list_available_turns(session.session_id)
    if turn not in available:
        print(f"[dot] Turn {turn} not found. Available: {available}")
        return
    host.rewind_to_turn(turn, session.session_id)
    print(f"[dot] Rewound to turn {turn}（对话与用户代码均已恢复）。")


def _safe_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """将 chunk 中的非 JSON 可序列化值转为字符串"""
    result = {}
    for k, v in chunk.items():
        if isinstance(v, dict):
            result[k] = {kk: str(vv) if not isinstance(vv, (str, int, float, bool, type(None), list, dict)) else vv
                         for kk, vv in v.items()}
        else:
            result[k] = v
    return result


if __name__ == "__main__":
    main()
    # setup_logging()
    # logger = get_logger(__name__)   # ✅ setup之后再拿logger
    # logger.info("dot agent starting, argv=%s", sys.argv)
