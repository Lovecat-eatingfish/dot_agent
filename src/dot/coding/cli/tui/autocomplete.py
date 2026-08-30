"""
dot.coding.cli.tui.autocomplete — 斜杠命令补全

纯函数式补全状态（对齐 tau 的 build_completion_state 设计）：
不依赖 UI 组件，输入文本 → CompletionState；渲染交给 app 的 Static。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dot.coding.commands import CommandRegistry


@dataclass
class CompletionState:
    """当前补全状态：候选列表 + 选中索引"""
    prefix: str = ""            # 正在补全的词（含 /）
    options: list[tuple[str, str]] = field(default_factory=list)  # (替换文本, 描述)
    selected: int = 0

    @property
    def active(self) -> bool:
        return bool(self.options)

    def move_next(self) -> None:
        if self.options:
            self.selected = (self.selected + 1) % len(self.options)

    def move_previous(self) -> None:
        if self.options:
            self.selected = (self.selected - 1) % len(self.options)

    @property
    def current(self) -> tuple[str, str] | None:
        if self.options and 0 <= self.selected < len(self.options):
            return self.options[self.selected]
        return None


def build_completion_state(text: str, registry: CommandRegistry) -> CompletionState:
    """根据输入文本构建补全状态

    仅处理斜杠命令补全（参数补全 v1 不做）：
      "/"      → 全部命令
      "/he"    → 前缀匹配
    """
    stripped = text.strip()
    if not stripped.startswith("/") or " " in stripped:
        return CompletionState()

    matches = sorted(registry.complete(stripped))
    if not matches:
        return CompletionState()

    options: list[tuple[str, str]] = []
    for name in matches:
        cmd = registry.get(name.removeprefix("/"))
        description = cmd.description if cmd else ""
        options.append((name + " ", description))
    return CompletionState(prefix=stripped, options=options)


def render_completions(state: CompletionState) -> str:
    """渲染补全下拉（纯文本 markup，供 Static 显示）"""
    if not state.active:
        return ""
    lines = []
    for i, (value, description) in enumerate(state.options[:10]):
        cursor = "› " if i == state.selected else "  "
        style = "reverse" if i == state.selected else "dim"
        lines.append(f"[{style}]{cursor}{value:<20}[/{style}] {description}")
    if len(state.options) > 10:
        lines.append(f"  … {len(state.options) - 10} more")
    return "\n".join(lines)
