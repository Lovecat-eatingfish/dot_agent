"""cost 模块测试：价格表 / UsageCollector / /model 运行时覆盖"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


def _fake_response(prompt_tokens: int, completion_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(usage_metadata={"input_tokens": prompt_tokens, "output_tokens": completion_tokens})


class TestEstimateCost:
    def test_known_model(self) -> None:
        from mokioclaw.reliability.cost import estimate_cost_usd

        # gpt-4o: $2.5/1M input, $10/1M output → 1M+1M = $12.5
        assert estimate_cost_usd("gpt-4o-2024-11-20", 1_000_000, 1_000_000) == pytest.approx(12.5)

    def test_unknown_model_fallback(self) -> None:
        from mokioclaw.reliability.cost import estimate_cost_usd

        cost = estimate_cost_usd("mystery-model-x", 500_000, 500_000)
        assert cost > 0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mokioclaw.reliability.cost import estimate_cost_usd

        monkeypatch.setenv("MOKIO_PRICE_INPUT_PER_1M", "1")
        monkeypatch.setenv("MOKIO_PRICE_OUTPUT_PER_1M", "2")
        assert estimate_cost_usd("whatever", 1_000_000, 1_000_000) == pytest.approx(3.0)


class TestUsageCollector:
    def test_record_and_snapshot(self) -> None:
        from mokioclaw.reliability.cost import UsageCollector

        collector = UsageCollector()
        collector.record_response(_fake_response(100, 50), model_name="test-model")
        collector.record_response(_fake_response(200, 80), model_name="test-model")

        snap = collector.snapshot()
        assert snap["prompt_tokens"] == 300
        assert snap["completion_tokens"] == 130
        assert snap["total_tokens"] == 430
        assert snap["per_model"]["test-model"]["prompt_tokens"] == 300
        assert snap["cost_usd"] > 0

    def test_delta_since(self) -> None:
        from mokioclaw.reliability.cost import UsageCollector

        collector = UsageCollector()
        collector.record_response(_fake_response(100, 50), model_name="m1")
        baseline = collector.snapshot()
        collector.record_response(_fake_response(30, 20), model_name="m1")

        delta = collector.delta_since(baseline)
        assert delta["prompt_tokens"] == 30
        assert delta["completion_tokens"] == 20
        assert delta["per_model"]["m1"]["prompt_tokens"] == 30

    def test_response_without_usage_skipped(self) -> None:
        from mokioclaw.reliability.cost import UsageCollector

        collector = UsageCollector()
        collector.record_response(SimpleNamespace(content="hi"))
        assert collector.snapshot()["total_tokens"] == 0

    def test_record_llm_usage_never_raises(self) -> None:
        from mokioclaw.reliability.cost import record_llm_usage

        record_llm_usage(None)  # 静默
        record_llm_usage(SimpleNamespace(usage_metadata="bad-type"))


class TestExtractUsage:
    def test_openai_response_metadata_fallback(self) -> None:
        from mokioclaw.reliability.cost import extract_usage

        resp = SimpleNamespace(
            usage_metadata=None,
            response_metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        assert extract_usage(resp) == (10, 5)


class TestActiveModelOverride:
    def test_set_and_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mokioclaw.providers.openai_provider import get_active_model, set_active_model

        monkeypatch.setattr("mokioclaw.providers.openai_provider._active_model_override", None, raising=False)
        set_active_model("test-override-model")
        assert get_active_model() == "test-override-model"
        set_active_model("")
        assert get_active_model() is None
        set_active_model(None)
        assert get_active_model() is None

    def test_active_model_name_prefers_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mokioclaw.providers.openai_provider import set_active_model
        from mokioclaw.reliability.cost import active_model_name

        monkeypatch.setenv("MODEL", "env-model")
        set_active_model("override-model")
        try:
            assert active_model_name() == "override-model"
        finally:
            set_active_model(None)
        assert active_model_name() == "env-model"
