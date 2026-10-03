"""python -m dot.coding.webui — WebUI 审批桥服务（默认 127.0.0.1:8002）"""
from __future__ import annotations

import argparse
from pathlib import Path

from dot.coding.host import CodingHost
from dot.coding.modes import AgentMode


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m dot.coding.webui",
                                     description="dot agent WebUI review bridge")
    parser.add_argument("-w", "--workspace", default=None, help="workspace dir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("-m", "--mode", default="auto", choices=["plan", "edit", "auto"])
    parser.add_argument("-r", "--resume", default=None, help="resume session id")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    host = CodingHost(workspace=workspace, mode=AgentMode.from_str(args.mode))
    if args.resume:
        if not host.resume_session(args.resume):
            print(f"session not found: {args.resume}, starting new session")

    from dot.coding.logging_config import setup as setup_logging

    setup_logging(workspace=workspace, level="INFO")
    print(f"dot agent review bridge: http://{args.host}:{args.port}/api/agent/health")
    serve_result = None
    from .server import serve

    serve(host, bind_host=args.host, port=args.port)
    del serve_result


if __name__ == "__main__":
    main()
