"""
dot.coding.ui.bridge — UiBridge Protocol + NullUiBridge

UiBridge 定义 UI 交互接口。
NullUiBridge 确保 extension 在无 UI 环境中正常运行。
"""
from __future__ import annotations

from typing import Any, Protocol


class UiBridge(Protocol):
    """UI 交互接口"""

    def confirm(self, message: str, *, default: bool = False) -> bool:
        """确认对话框"""
        ...

    def input(self, prompt: str) -> str:
        """文本输入"""
        ...

    def print(self, message: str) -> None:
        """输出消息"""
        ...

    def print_error(self, message: str) -> None:
        """输出错误"""
        ...

    def print_warning(self, message: str) -> None:
        """输出警告"""
        ...


class NullUiBridge:
    """无 UI 环境的降级实现

    每个方法都有合理默认返回值，
    extension 在无 UI 环境（print mode、测试）中正常运行。
    """

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return default

    def input(self, prompt: str) -> str:
        return ""

    def print(self, message: str) -> None:
        pass

    def print_error(self, message: str) -> None:
        pass

    def print_warning(self, message: str) -> None:
        pass


class ConsoleUiBridge:
    """控制台 UI 实现"""

    def confirm(self, message: str, *, default: bool = False) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            answer = input(message + suffix).strip().lower()
            if not answer:
                return default
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def input(self, prompt: str) -> str:
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    def print(self, message: str) -> None:
        print(message)

    def print_error(self, message: str) -> None:
        print(f"ERROR: {message}")

    def print_warning(self, message: str) -> None:
        print(f"WARNING: {message}")
