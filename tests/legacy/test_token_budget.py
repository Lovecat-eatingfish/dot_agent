"""引擎层恢复原语测试（对齐 Claude Code）

覆盖 5 项：
1. BudgetTracker 预算追踪 + 收益递减检测
2. OutputTokenRecovery 两阶段恢复（escalate → resume → 用尽）
3. PromptTooLongRecovery 单次恢复
4. filter_unresolved_tool_uses 悬空 tool_use 清洗
5. invoke_with_fallback 模型降级回退 + FallbackTriggeredError

外加 is_truncated / is_prompt_too_long_error 判定。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mokioclaw.reliability.token_budget import (
    BudgetTracker,
    DIMINISHING_MIN_CONTINUATIONS,
    DIMINISHING_THRESHOLD,
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    OutputTokenRecovery,
    PromptTooLongRecovery,
    filter_unresolved_tool_uses,
    is_prompt_too_long_error,
    is_truncated,
)


# ===== 1. BudgetTracker =====

def _resp(output_tokens: int, finish_reason: str = "stop") -> AIMessage:
    return AIMessage(
        content="ok",
        response_metadata={"finish_reason": finish_reason},
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": output_tokens,
            "total_tokens": 10 + output_tokens,
        },
    )


def test_budget_tracker_no_budget_never_stops():
    """budget=None（子 Agent）永远不停止"""
    bt = BudgetTracker(budget=None)
    for _ in range(50):
        bt.account(_resp(1000))
    stop, reason = bt.check()
    assert stop is False
    assert reason == ""


def test_budget_tracker_threshold_stop():
    """累计达预算 90% → 停止"""
    bt = BudgetTracker(budget=1000)
    # 第一次 check 设基线，不触发
    bt.account(_resp(400))  # 400 tokens
    stop, _ = bt.check()
    assert stop is False
    # 第二次：累计 950 > 900(90%) → 停止
    bt.account(_resp(550))
    stop, reason = bt.check()
    assert stop is True
    assert reason == "budget_threshold"


def test_budget_tracker_diminishing_returns():
    """连续 N 次增量都 < 阈值 → 收益递减停止"""
    bt = BudgetTracker(budget=1_000_000)  # 预算很大，不触发阈值
    # 首次设基线
    bt.account(_resp(1000))
    bt.check()
    # 后续每次只增 10 token（< DIMINISHING_THRESHOLD=500）
    # 需要 continuation_count >= DIMINISHING_MIN_CONTINUATIONS(3)
    stopped = False
    for _ in range(DIMINISHING_MIN_CONTINUATIONS + 2):
        bt.account(_resp(10))
        stop, reason = bt.check()
        if stop:
            stopped = True
            assert reason == "diminishing_returns"
            break
    assert stopped, "should detect diminishing returns"


def test_budget_tracker_diminishing_needs_two_consecutive_small():
    """上一次增量大、本次小 → 不应触发收益递减（需连续两次都小）

    回归 C3：原实现先覆盖 last_delta_tokens 再比较，等价于只看一次小增量。
    """
    bt = BudgetTracker(budget=1_000_000)
    bt.account(_resp(1000))  # 基线
    bt.check()
    # 多轮大增量，积累 continuation_count
    for _ in range(DIMINISHING_MIN_CONTINUATIONS + 1):
        bt.account(_resp(1000))  # 每轮 +1000（大增量）
        stop, _ = bt.check()
        assert stop is False, "big delta should not trigger diminishing"
    # 本轮小增量（上一轮仍是大增量）→ 不应触发
    bt.account(_resp(10))
    stop, _ = bt.check()
    assert stop is False, "single small delta after big should not trigger"
    # 再一轮小增量（连续两次小）→ 触发
    bt.account(_resp(10))
    stop, reason = bt.check()
    assert stop is True
    assert reason == "diminishing_returns"


# ===== 2. OutputTokenRecovery =====

def test_output_recovery_escalate_then_resume_then_exhausted():
    rec = OutputTokenRecovery()
    # 阶段一：escalate
    r1 = rec.on_truncated()
    assert r1 == {"action": "escalate"}
    assert rec.escalated is True
    assert rec.max_output_tokens_override == 64_000
    # 阶段二：resume，最多 MAX_OUTPUT_TOKENS_RECOVERY_LIMIT(3) 次
    for i in range(MAX_OUTPUT_TOKENS_RECOVERY_LIMIT):
        r = rec.on_truncated()
        assert r["action"] == "resume"
        assert "message" in r
    # 用尽
    r_end = rec.on_truncated()
    assert r_end is None


def test_output_recovery_reset_for_new_turn():
    rec = OutputTokenRecovery()
    rec.on_truncated()  # escalate
    rec.on_truncated()  # resume 1
    rec.on_truncated()  # resume 2
    assert rec.recovery_count == 2
    # 新的一轮重置 resume 计数，但保留 escalated
    rec.reset_for_new_turn()
    assert rec.recovery_count == 0
    assert rec.escalated is True
    # 仍可 resume
    r = rec.on_truncated()
    assert r["action"] == "resume"


def test_is_truncated():
    assert is_truncated(_resp(10, finish_reason="length")) is True
    assert is_truncated(_resp(10, finish_reason="stop")) is False
    assert is_truncated(_resp(10, finish_reason="tool_calls")) is False


def test_classifier_prompt_template_formats_without_keyerror():
    """回归 C1：分类器 prompt 模板含字面 JSON 花括号，.format() 不应抛 KeyError"""
    from mokioclaw.security.classifier import _CLASSIFIER_PROMPT, _HANDOFF_PROMPT

    # 不抛异常即通过（字面 { } 已转义为 {{ }}）
    _CLASSIFIER_PROMPT.format(tool_name="BashTool", tool_args="{}", context="x")
    _HANDOFF_PROMPT.format(task="t", summary="s", context="c")


def test_handoff_fast_path_allows_workspace_cleanup():
    """回归 M8：handoff fast-path 对 rm -rf <工作区路径> 不应误判 DENY"""
    import os
    os.environ.pop("MOKIO_AUTO_CLASSIFIER", None)
    from mokioclaw.security.classifier import classify_handoff

    # 工作区内常见清理 → ALLOW
    assert classify_handoff("clean", "ran rm -rf node_modules")[0].value == "allow"
    assert classify_handoff("clean", "ran rm -rf build dist")[0].value == "allow"
    # 毁灭性目标 → DENY
    assert classify_handoff("nuke", "ran rm -rf /")[0].value == "deny"
    assert classify_handoff("nuke", "ran rm -rf ~")[0].value == "deny"
    assert classify_handoff("nuke", "ran rm -rf /home")[0].value == "deny"


# ===== 3. PromptTooLongRecovery =====

def test_prompt_too_long_recovery_single_shot():
    rec = PromptTooLongRecovery()
    assert rec.should_recover() is True
    rec.mark_attempted()
    assert rec.should_recover() is False


def test_is_prompt_too_long_error_markers():
    assert is_prompt_too_long_error(RuntimeError("This model's maximum context length is 128000 tokens.")) is True
    assert is_prompt_too_long_error(RuntimeError("prompt is too long")) is True
    assert is_prompt_too_long_error(RuntimeError("413 Request Entity Too Large")) is True
    assert is_prompt_too_long_error(RuntimeError("rate limit exceeded")) is False
    assert is_prompt_too_long_error(ValueError("invalid api key")) is False


# ===== 4. filter_unresolved_tool_uses =====

def test_filter_unresolved_injects_placeholder():
    """AIMessage 带 tool_calls 但无对应 ToolMessage → 补占位"""
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "BashTool", "args": {"command": "ls"}}],
    )
    messages = [HumanMessage(content="run"), ai]
    result = filter_unresolved_tool_uses(messages)
    # 应补一条 ToolMessage
    assert len(result) == 3
    placeholder = result[-1]
    assert isinstance(placeholder, ToolMessage)
    assert placeholder.tool_call_id == "call_1"
    assert placeholder.name == "BashTool"


def test_filter_unresolved_no_op_when_paired():
    """tool_use 已有对应 tool_result → 不补"""
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "BashTool", "args": {}}],
    )
    tm = ToolMessage(content="done", tool_call_id="call_1", name="BashTool")
    messages = [HumanMessage(content="run"), ai, tm]
    result = filter_unresolved_tool_uses(messages)
    assert result == messages


def test_filter_unresolved_multiple_dangling():
    """多个悬空 tool_call 都补占位"""
    ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "c1", "name": "BashTool", "args": {}},
            {"id": "c2", "name": "FileReadTool", "args": {}},
        ],
    )
    messages = [ai]
    result = filter_unresolved_tool_uses(messages)
    # ai + 2 占位
    assert len(result) == 3
    ids = [m.tool_call_id for m in result if isinstance(m, ToolMessage)]
    assert set(ids) == {"c1", "c2"}


def test_filter_unresolved_empty():
    assert filter_unresolved_tool_uses([]) == []


# ===== 5. invoke_with_fallback =====

class _FakeModel:
    """简易 fake model：invoke 按预设行为返回 / 抛异常"""

    def __init__(self, *, invoke_raises: Exception | None = None, return_value=None):
        self._invoke_raises = invoke_raises
        self._return_value = return_value
        self.invoke_count = 0

    def invoke(self, messages):
        self.invoke_count += 1
        if self._invoke_raises is not None:
            raise self._invoke_raises
        return self._return_value


class _BadRequestError(Exception):
    """模拟 openai.BadRequestError（按类名判定）"""


def test_invoke_with_fallback_primary_succeeds():
    """主模型成功 → 不降级"""
    primary = _FakeModel(return_value="primary-ok")
    with patch("mokioclaw.providers.openai_provider.create_model") as mock_create:
        from mokioclaw.providers.openai_provider import invoke_with_fallback
        result = invoke_with_fallback(primary, ["m"], fallbacks=["fb-a"])
    assert result == "primary-ok"
    assert primary.invoke_count == 1
    mock_create.assert_not_called()


def test_invoke_with_fallback_falls_back():
    """主模型抛非 400 异常 → 降级到 fallback 模型"""
    primary = _FakeModel(invoke_raises=RuntimeError("connection reset"))
    fb_model = _FakeModel(return_value="fallback-ok")

    with patch("mokioclaw.providers.openai_provider.create_model", return_value=fb_model) as mock_create:
        from mokioclaw.providers.openai_provider import invoke_with_fallback
        result = invoke_with_fallback(primary, ["m"], fallbacks=["fb-a"])

    assert result == "fallback-ok"
    assert primary.invoke_count == 1
    mock_create.assert_called_once_with(model="fb-a")


def test_invoke_with_fallback_no_fallbacks_reraises():
    """无 fallback 列表且主模型失败 → 抛原异常"""
    primary = _FakeModel(invoke_raises=RuntimeError("boom"))
    from mokioclaw.providers.openai_provider import invoke_with_fallback
    with pytest.raises(RuntimeError, match="boom"):
        invoke_with_fallback(primary, ["m"], fallbacks=[])


def test_invoke_with_fallback_bad_request_not_degraded():
    """400 类错误不降级，直接抛"""
    primary = _FakeModel(invoke_raises=_BadRequestError("context_length_exceeded"))
    with patch("mokioclaw.providers.openai_provider.create_model") as mock_create:
        from mokioclaw.providers.openai_provider import invoke_with_fallback
        with pytest.raises(_BadRequestError):
            invoke_with_fallback(primary, ["m"], fallbacks=["fb-a"])
    mock_create.assert_not_called()


def test_invoke_with_fallback_all_fail_raises_fallback_triggered():
    """主模型 + 所有 fallback 都失败 → FallbackTriggeredError"""
    primary = _FakeModel(invoke_raises=RuntimeError("primary down"))
    fb_model = _FakeModel(invoke_raises=RuntimeError("fb down"))

    with patch("mokioclaw.providers.openai_provider.create_model", return_value=fb_model):
        from mokioclaw.providers.openai_provider import (
            FallbackTriggeredError,
            invoke_with_fallback,
        )
        with pytest.raises(FallbackTriggeredError):
            invoke_with_fallback(primary, ["m"], fallbacks=["fb-a", "fb-b"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
