"""
dot.coding.cli.tui.banner — 启动横幅

启动时把 assets/logo.png（像素猫）以半块字符画形式渲染到 TUI 顶部，
风格对齐 Claude Code 的启动头图。像素画由 scripts/gen_tui_banner.py
预生成为 _banner_art.py，运行时零依赖（不需要 Pillow）。
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from ._banner_art import BANNER_ART

ORANGE = "#f1662a"
BLUE = "#3b6fd8"


def _app_version() -> str:
    try:
        return version("dot-agent")
    except PackageNotFoundError:
        return ""


def build_banner(notes: list[str] | None = None) -> str:
    """构造横幅 markup：像素画 + 标题行 + 提示行 + 备注（如 resume 信息）"""
    ver = _app_version()
    title = f"[bold {ORANGE}]dot agent[/bold {ORANGE}]"
    if ver:
        title += f" [dim]{ver}[/dim]"

    lines = [
        BANNER_ART,
        "",
        f"  {title} [dim]·[/dim] [dim]coding agent[/dim]",
        (
            "  [dim]/help commands · Tab mode · Ctrl+O tool output · "
            "Ctrl+T thinking · Esc interrupt[/dim]"
        ),
    ]
    for note in notes or []:
        lines.append(f"  [dim]·[/dim] {note}")
    return "\n".join(lines)
