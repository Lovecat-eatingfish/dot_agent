"""
dot.coding.extensions — 扩展系统

职责：
  - ExtensionAPI: 注册接口（register_tool / register_command / on_event）
  - ExtensionLoader: 扫描扩展目录
  - ExtensionRuntime: 生命周期、事件分发、hook 链
  - ExtensionGeneration: liveness token（热重载后旧引用立即失效）
"""
from __future__ import annotations
