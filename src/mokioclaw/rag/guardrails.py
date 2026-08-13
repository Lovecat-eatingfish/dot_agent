"""输出护栏（Guardrails）：对 LLM 生成答案做安全/合规检查

对齐生产级 RAG 的「输出安全」：答案输出前过一遍护栏，拦截：
1. 敏感信息泄漏（API key / token / 密码 / 手机号 / 邮箱等 PII）
2. 越界话题（与上下文无关的指令注入响应，如 prompt injection 导致 LLM 越权）
3. 无引用断言（答案声称事实但无 [n] 引用 → 标记需人工复核）

减法设计：
- 规则 fast-path 为主（正则 + 关键词），不引 LLM 做二次判断
- 命中护栏 → 替换为拒答话术 + 记 trace 降级
- 不做：复杂 toxicity 分类、多语言 NSFW（需模型，留给扩展）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 敏感信息正则
_SECRET_PATTERNS = [
    # API key / token（常见前缀 + 长串）
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential)\b\s*[:=]\s*\S{8,}"), "secret_leak"),
    # sk- 开头的 OpenAI key
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "api_key_leak"),
    # 手机号（中国大陆）
    (re.compile(r"\b1[3-9]\d{9}\b"), "phone_leak"),
    # 邮箱
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "email_leak"),
    # 身份证号（18 位）
    (re.compile(r"\b\d{17}[\dXx]\b"), "id_card_leak"),
]

# 越界话题关键词（prompt injection 常见产出）
_OUT_OF_SCOPE_KEYWORDS = [
    "ignore previous instructions",
    "disregard the above",
    "you are now",
    "new instructions:",
    "system prompt:",
]


@dataclass
class GuardrailResult:
    """护栏检查结果"""
    passed: bool
    reason: str = ""
    violations: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.violations is None:
            self.violations = []


_BLOCKED_ANSWER = (
    "I cannot provide this response as it was flagged by output guardrails. "
    "Please refine your question."
)


def check_answer(answer: str, *, has_citations: bool = False) -> GuardrailResult:
    """检查 LLM 生成的答案

    Args:
        answer: LLM 生成的答案文本
        has_citations: 是否期望有引用（事实性断言应带引用）

    Returns:
        GuardrailResult: passed=False 表示被拦截
    """
    violations: list[str] = []

    # 1. 敏感信息泄漏
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(answer):
            violations.append(label)

    # 2. 越界话题（prompt injection 产出）
    lower = answer.lower()
    for kw in _OUT_OF_SCOPE_KEYWORDS:
        if kw in lower:
            violations.append("out_of_scope")
            break

    if violations:
        return GuardrailResult(
            passed=False,
            reason=f"guardrail violations: {', '.join(violations)}",
            violations=violations,
        )
    return GuardrailResult(passed=True)


def apply_guardrails(answer: str, *, has_citations: bool = False) -> tuple[str, GuardrailResult]:
    """应用护栏：检查 + 必要时替换

    Returns:
        (final_answer, result)
        - 被拦截时 final_answer 为拒答话术
    """
    result = check_answer(answer, has_citations=has_citations)
    if not result.passed:
        return _BLOCKED_ANSWER, result
    return answer, result
