"""
轻量意图路由（对齐 docs/agent.md：去掉重型意图 LLM，改启发式 + 工具驱动）

规则：
- 明显闲聊短句 → chat
- 斜杠命令 / 编码动作词 / 文件路径 → workflow
- 默认 workflow（复杂任务交给 planner + tools）
"""
from __future__ import annotations

import re

_CHAT_PATTERNS = [
    re.compile(r"^(你好|您好|hi|hello|hey|thanks|thank you|谢谢|再见|bye)\s*[!！.?？]*$", re.I),
    re.compile(r"^(你是谁|你能做什么|help|帮助)\s*[?？]*$", re.I),
]

_WORKFLOW_HINTS = re.compile(
    r"(写|改|修|实现|创建|删除|重构|搜索|查|分析|测试|部署|调试|fix|implement|create|edit|refactor|"
    r"search|debug|test|build|deploy|\.py|\.ts|\.js|\.md|/|\\)",
    re.I,
)


def classify_intent(task: str) -> tuple[str, str, float]:
    """返回 (route, reason, confidence)，route ∈ {chat, workflow}"""
    text = (task or "").strip()
    if not text:
        return "chat", "empty input", 0.9

    for pattern in _CHAT_PATTERNS:
        if pattern.match(text):
            return "chat", "matched casual greeting/help pattern", 0.95

    if text.startswith("/"):
        return "workflow", "slash command / skill style input", 0.9

    if len(text) <= 20 and not _WORKFLOW_HINTS.search(text):
        # 短句且无动作词，倾向 chat
        return "chat", "short utterance without task verbs", 0.7

    if _WORKFLOW_HINTS.search(text):
        return "workflow", "task verbs or file hints detected", 0.85

    # 默认走工作流：由 planner + tools 自己判断
    return "workflow", "default to tool-driven workflow", 0.6
