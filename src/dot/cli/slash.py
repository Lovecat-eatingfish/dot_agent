"""
斜杠命令体系（对齐设计文档 §5）

所有 `/` 开头命令为本地终端指令：不进入 message 上下文、不消耗 token、不发给 LLM。
支持 Tab 命令补全、非法命令容错提示（红色提示，不闪退）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .session_bridge import SessionBridge


# ============================================================
# 结果类型（TUI 据此决定如何展示）
# ============================================================

@dataclass
class SlashResult:
    """斜杠命令执行结果"""
    # message: 在聊天/日志面板输出文本；toast: 顶部短暂提示；其余为动作
    kind: str = "message"  # message | toast | clear_screen | reset_session | quit | none
    text: str = ""
    # toast 级别（info/warn/error），用于红色容错提示
    level: str = "info"


# ============================================================
# 命令定义
# ============================================================

@dataclass
class SlashCommand:
    """单个斜杠命令"""
    name: str                       # 不含 /，如 "mode"
    usage: str                      # 补全/帮助用，如 "/mode [agent|chat|code]"
    description: str
    # handler(bridge, args) -> SlashResult
    handler: Callable[["SessionBridge", str], SlashResult] = field(repr=False)


def _cmd_help(bridge: "SessionBridge", args: str) -> SlashResult:
    return SlashResult(kind="message", text=build_help_text())


def _cmd_clear(bridge: "SessionBridge", args: str) -> SlashResult:
    return SlashResult(kind="clear_screen")


def _cmd_reset(bridge: "SessionBridge", args: str) -> SlashResult:
    info = bridge.reset_session()
    return SlashResult(kind="reset_session", text=info, level="info")


def _cmd_mode(bridge: "SessionBridge", args: str) -> SlashResult:
    from .modes import normalize_mode
    arg = args.strip()
    if not arg:
        cur = bridge.get_mode()
        return SlashResult(kind="toast", text=f"当前模式: {cur}", level="info")
    mode = normalize_mode(arg)
    bridge.set_mode(mode)
    return SlashResult(kind="toast", level="info", text=f"已切换到 {mode} 模式（下轮生效）")


def _cmd_compact(bridge: "SessionBridge", args: str) -> SlashResult:
    sub = args.strip().lower()
    if sub == "force":
        report = bridge.force_compact()
        return SlashResult(kind="message", text=f"[compact/force] {report}")
    if sub in ("status", ""):
        return SlashResult(kind="message", text=bridge.compact_status())
    return SlashResult(
        kind="toast", level="error",
        text=f"未知子命令: /compact {args}（可选: force | status）",
    )


def _cmd_workspace(bridge: "SessionBridge", args: str) -> SlashResult:
    arg = args.strip()
    if not arg:
        return SlashResult(kind="message", text=f"当前工作目录: {bridge.get_workspace()}")
    try:
        bridge.switch_workspace(arg)
        return SlashResult(kind="toast", level="info", text=f"工作目录已切换: {arg}")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"切换工作目录失败: {exc}")


def _cmd_mcp(bridge: "SessionBridge", args: str) -> SlashResult:
    sub = args.strip().lower()
    if sub == "restart":
        report = bridge.mcp_restart()
        return SlashResult(kind="message", text=f"[mcp/restart] {report}")
    if sub in ("list", ""):
        return SlashResult(kind="message", text=bridge.mcp_list())
    return SlashResult(
        kind="toast", level="error",
        text=f"未知子命令: /mcp {args}（可选: list | restart）",
    )


def _cmd_save(bridge: "SessionBridge", args: str) -> SlashResult:
    name = args.strip() or None
    try:
        sid = bridge.save_session(name)
        return SlashResult(kind="toast", level="info", text=f"会话已保存: {sid}")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"保存失败: {exc}")


def _cmd_load(bridge: "SessionBridge", args: str) -> SlashResult:
    name = args.strip()
    if not name:
        return SlashResult(kind="toast", level="error", text="用法: /load <session_id>")
    try:
        info = bridge.load_session(name)
        return SlashResult(kind="toast", level="info", text=f"已加载会话: {info}")
    except Exception as exc:
        return SlashResult(kind="toast", level="error", text=f"加载失败: {exc}")


def _cmd_exit(bridge: "SessionBridge", args: str) -> SlashResult:
    return SlashResult(kind="quit")


def _cmd_config(bridge: "SessionBridge", args: str) -> SlashResult:
    """查看/修改运行时配置"""
    parts = args.strip().split(None, 2)
    config = getattr(bridge, "config", None)
    if config is None:
        return SlashResult(kind="toast", level="error", text="配置未加载")

    if not parts:
        # /config — 显示全部配置
        masked = config.masked()
        lines = ["[config]"]
        for k, v in masked.items():
            lines.append(f"  {k}: {v}")
        return SlashResult(kind="message", text="\n".join(lines))

    if parts[0] == "set" and len(parts) >= 3:
        # /config set key value
        key = parts[1]
        value = parts[2]
        config.set(key, value)
        return SlashResult(kind="toast", level="info", text=f"已设置 {key} = {value}（下轮生效）")

    return SlashResult(
        kind="toast", level="error",
        text=f"用法: /config 或 /config set key value",
    )


# ============================================================
# 命令注册表
# ============================================================

_COMMANDS: list[SlashCommand] = [
    SlashCommand("help", "/help", "展示全部快捷键、斜杠命令帮助", _cmd_help),
    SlashCommand("clear", "/clear", "清空聊天界面 UI，保留内存会话数据", _cmd_clear),
    SlashCommand("reset", "/reset", "重建全新 Session，清空消息/归档/压缩状态", _cmd_reset),
    SlashCommand("config", "/config [set key value]", "查看/修改运行时配置", _cmd_config),
    SlashCommand("mode", "/mode [agent|chat|code]", "查看/切换运行模式", _cmd_mode),
    SlashCommand("compact", "/compact force|status", "手动强制压缩 / 查看压缩状态", _cmd_compact),
    SlashCommand("workspace", "/workspace [path]", "查看/切换工作目录", _cmd_workspace),
    SlashCommand("mcp", "/mcp list|restart", "查看 MCP 工具列表 / 重启 MCP 服务", _cmd_mcp),
    SlashCommand("save", "/save [name]", "持久化保存当前会话", _cmd_save),
    SlashCommand("load", "/load [name]", "加载本地历史会话", _cmd_load),
    SlashCommand("exit", "/exit", "退出 TUI 终端", _cmd_exit),
]

# name -> command 快查
_COMMAND_MAP: dict[str, SlashCommand] = {c.name: c for c in _COMMANDS}


# ============================================================
# 解析与执行
# ============================================================

def is_slash_input(text: str) -> bool:
    """是否为斜杠命令（以 / 开头且非空）"""
    return bool(text) and text.lstrip().startswith("/")


def parse(text: str) -> tuple[str, str] | None:
    """解析斜杠命令 → (command_name, args)，非斜杠命令返回 None"""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return name, args


def execute(bridge: "SessionBridge", text: str) -> SlashResult:
    """执行斜杠命令；非法命令返回红色容错提示（不抛异常）"""
    parsed = parse(text)
    if parsed is None:
        return SlashResult(kind="toast", level="error", text="无效命令")
    name, args = parsed
    cmd = _COMMAND_MAP.get(name)
    if cmd is None:
        return SlashResult(
            kind="toast", level="error",
            text=f"未知命令: /{name}（输入 /help 查看可用命令）",
        )
    try:
        return cmd.handler(bridge, args)
    except Exception as exc:
        return SlashResult(
            kind="toast", level="error",
            text=f"/{name} 执行出错: {exc}",
        )


def complete(text: str) -> list[str]:
    """Tab 补全：返回匹配的命令 usage 列表"""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return []
    body = stripped[1:]
    # 已输入空格 → 不补全命令名（交由具体参数补全，本期略）
    if " " in body:
        return []
    prefix = body.lower()
    matches = [c for c in _COMMANDS if c.name.startswith(prefix)]
    if not matches:
        return []
    if len(matches) == 1:
        return [f"/{matches[0].name} "]
    return [f"/{c.name}" for c in matches]


def build_help_text() -> str:
    """构造 /help 帮助文本（快捷键 + 斜杠命令）"""
    from .keybindings import SHORTCUTS_HELP

    lines = ["[bold]快捷键[/bold]", SHORTCUTS_HELP, "", "[bold]斜杠命令[/bold]"]
    for c in _COMMANDS:
        lines.append(f"  {c.usage:<28} {c.description}")
    return "\n".join(lines)


def render_for_repl(result: "SlashResult") -> str:
    """把 SlashResult 转成 ANSI 字符串，供 DotREPL 主栏渲染"""
    from .renderer import RichRenderer

    renderer = RichRenderer(width=100)
    kind = result.kind
    text = result.text or ""
    if kind == "message":
        return renderer.render_markdown(text)
    if kind == "toast":
        style = "bold red" if result.level == "error" else "bold yellow" if result.level == "warn" else "bold cyan"
        return renderer.render_text(text, style=style)
    if kind == "clear_screen":
        return ""
    return ""
