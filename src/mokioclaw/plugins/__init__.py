"""插件 / 本地 marketplace（对齐 Claude Code plugins）"""

from mokioclaw.plugins.marketplace import (
    disable_plugin,
    enable_plugin,
    install_plugin,
    list_catalog,
    list_installed,
    uninstall_plugin,
)

__all__ = [
    "disable_plugin",
    "enable_plugin",
    "install_plugin",
    "list_catalog",
    "list_installed",
    "uninstall_plugin",
]
