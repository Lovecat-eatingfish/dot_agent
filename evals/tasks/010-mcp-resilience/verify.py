# -*- coding: utf-8 -*-
"""判分：MCP 服务器不可达时 agent 仍正常完成任务（exit 0）"""
import os
assert os.environ.get("AGENT_EXIT_CODE") == "0", \
    "agent 退出码 %s（MCP 失败导致崩溃/失败）" % os.environ.get("AGENT_EXIT_CODE")
print("PASS")
