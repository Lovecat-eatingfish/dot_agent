"""
dot agent 终端入口（委托 cli.app Typer 应用）

用法（对齐设计文档 §11，用 uv 启动）：
    uv run agent                      # TUI 交互模式（主入口）
    uv run agent interactive          # 同上
    uv run agent console              # 控制台调试模式（旧版调试命令）
    uv run agent run "写个快排"        # 一次性非交互任务
    uv run agent config show           # 查看环境配置
    uv run agent mcp list             # 查看 MCP 工具
"""
from __future__ import annotations

from dot.cli.app import app


def main() -> None:
    """控制台入口"""
    app()


if __name__ == "__main__":
    main()
