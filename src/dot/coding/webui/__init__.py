"""
dot.coding.webui — WebUI 环境审批适配（服务端闭环）

权限设计文档 §10.4.1 WebUI 环境的落地：
- bridge : ASK 挂起/恢复桥（零框架依赖，可独立测试）
- routes : FastAPI 路由（prompt / approvals / review）
- server : FastAPI 装配 + uvicorn 拉起；`python -m dot.coding.webui` 启动

前端页面渲染待 WebUI 立项后补充。
"""
from __future__ import annotations

from .bridge import ApprovalRequest, WebUIApprovalBridge

__all__ = ["ApprovalRequest", "WebUIApprovalBridge"]
