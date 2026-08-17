"""Token 费用统计（对齐 Claude Code /cost 美元计价）

三个职责：
1. 价格表：常见模型每 1M token 的 input/output 单价（美元），支持环境变量覆盖
2. UsageCollector：进程级 usage 收集器，所有 LLM 调用点调用 record_response()，
   免去把 TraceRecorder 一路穿透到 nodes/agents 的 plumbing
3. estimate_cost_usd：按模型名匹配价格表估算美元费用

用法：
    from mokioclaw.reliability.cost import usage_collector, estimate_cost_usd
    usage_collector().record_response(response)   # 每次 invoke 后
    delta = usage_collector().delta_since(baseline)  # TraceRecorder 差分统计

价格说明：
- 价格按"模式子串"匹配（如 "gpt-4o" 命中所有 gpt-4o 变体），第一个命中生效
- 缓存命中不计（教学版简化），只按 prompt/completion 原价估算
- 未知模型按 UNKNOWN 兜底价（可用 MOKIO_PRICE_INPUT_PER_1M / OUTPUT 覆盖）
"""
from __future__ import annotations

import os
import threading
from typing import Any

# 每条：(模型名子串, input $/1M tokens, output $/1M tokens)，先命中先用
_PRICE_TABLE_PER_1M: list[tuple[str, float, float]] = [
    # OpenAI
    ("o3", 2.0, 8.0),
    ("o4-mini", 1.1, 4.4),
    ("gpt-4.1", 2.0, 8.0),
    ("gpt-4o-mini", 0.15, 0.6),
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4-turbo", 10.0, 30.0),
    ("gpt-4", 30.0, 60.0),
    ("gpt-3.5", 0.5, 1.5),
    # Anthropic
    ("claude-oppus", 15.0, 75.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 0.8, 4.0),
    # DeepSeek
    ("deepseek-reasoner", 0.55, 2.19),
    ("deepseek-chat", 0.27, 1.1),
    ("deepseek", 0.27, 1.1),
    # Qwen / GLM / Kimi / Gemini
    ("qwen-max", 1.6, 6.4),
    ("qwen-plus", 0.4, 1.2),
    ("qwen-turbo", 0.05, 0.2),
    ("qwen", 0.5, 1.5),
    ("glm-4", 0.14, 0.42),
    ("glm", 0.14, 0.42),
    ("kimi", 0.6, 2.5),
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.5-flash", 0.3, 2.5),
    ("gemini", 0.075, 0.3),
]

# 未知模型兜底价（$ / 1M tokens）
_UNKNOWN_PRICE = (1.0, 3.0)

_MODEL_NAME_FALLBACK = "(unknown)"


def _price_for(model_name: str) -> tuple[float, float]:
    """按子串匹配价格表；支持环境变量整体覆盖"""
    env_in = os.getenv("MOKIO_PRICE_INPUT_PER_1M")
    env_out = os.getenv("MOKIO_PRICE_OUTPUT_PER_1M")
    if env_in or env_out:
        try:
            return (float(env_in) if env_in else _UNKNOWN_PRICE[0], float(env_out) if env_out else _UNKNOWN_PRICE[1])
        except ValueError:
            pass
    lowered = (model_name or "").lower()
    for pattern, price_in, price_out in _PRICE_TABLE_PER_1M:
        if pattern in lowered:
            return price_in, price_out
    return _UNKNOWN_PRICE


def estimate_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算一次调用（或累计）的美元费用，保留 6 位小数"""
    price_in, price_out = _price_for(model_name)
    return round(prompt_tokens / 1_000_000 * price_in + completion_tokens / 1_000_000 * price_out, 6)


def extract_usage(response: Any) -> tuple[int, int]:
    """从 LangChain AIMessage 提取 (input_tokens, output_tokens)

    兼容 usage_metadata（LangChain 标准）与 response_metadata.token_usage（OpenAI 原生）。
    """
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    meta = getattr(response, "response_metadata", None)
    token_usage = meta.get("token_usage") if isinstance(meta, dict) else None
    if isinstance(token_usage, dict):
        return int(token_usage.get("prompt_tokens", 0) or 0), int(token_usage.get("completion_tokens", 0) or 0)
    return 0, 0


class UsageCollector:
    """进程级 usage 累计器（线程安全）

    所有 agent 循环 / 节点直接 invoke 后调用 record_response()；
    TraceRecorder 在 start() 记基线、end() 取差分，得到本 trace 的用量。
    模型名取自 provider 的 active model（/model 切换后自动跟随）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prompt_tokens = 0
        self._completion_tokens = 0
        # 模型名 → (prompt, completion)，用于多模型混用时分别计价
        self._per_model: dict[str, tuple[int, int]] = {}

    def record_response(self, response: Any, *, model_name: str | None = None) -> None:
        """记录一次 LLM 响应的 usage（无 usage 字段时静默跳过）"""
        prompt, completion = extract_usage(response)
        if prompt <= 0 and completion <= 0:
            return
        name = model_name or active_model_name()
        with self._lock:
            self._prompt_tokens += prompt
            self._completion_tokens += completion
            prev_p, prev_c = self._per_model.get(name, (0, 0))
            self._per_model[name] = (prev_p + prompt, prev_c + completion)

    def snapshot(self) -> dict[str, Any]:
        """当前累计用量（含按模型拆分与美元估算）"""
        with self._lock:
            per_model = {name: dict(prompt_tokens=p, completion_tokens=c) for name, (p, c) in self._per_model.items()}
            totals = dict(
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                total_tokens=self._prompt_tokens + self._completion_tokens,
            )
        totals["cost_usd"] = round(sum(estimate_cost_usd(m, v["prompt_tokens"], v["completion_tokens"]) for m, v in per_model.items()), 6)
        for name, usage in per_model.items():
            usage["cost_usd"] = estimate_cost_usd(name, usage["prompt_tokens"], usage["completion_tokens"])
        totals["per_model"] = per_model
        return totals

    def delta_since(self, baseline: dict[str, Any] | None) -> dict[str, Any]:
        """相对基线的增量用量（TraceRecorder 用它隔离本 trace 的统计）"""
        current = self.snapshot()
        if not baseline:
            return current
        base_prompt = int(baseline.get("prompt_tokens", 0) or 0)
        base_completion = int(baseline.get("completion_tokens", 0) or 0)
        delta = dict(
            prompt_tokens=max(0, current["prompt_tokens"] - base_prompt),
            completion_tokens=max(0, current["completion_tokens"] - base_completion),
        )
        delta["total_tokens"] = delta["prompt_tokens"] + delta["completion_tokens"]
        base_models = baseline.get("per_model", {}) if isinstance(baseline.get("per_model"), dict) else {}
        per_model: dict[str, dict[str, int]] = {}
        for name, usage in current["per_model"].items():
            base = base_models.get(name) if isinstance(base_models.get(name), dict) else {}
            p = max(0, usage["prompt_tokens"] - int(base.get("prompt_tokens", 0) or 0))
            c = max(0, usage["completion_tokens"] - int(base.get("completion_tokens", 0) or 0))
            if p or c:
                per_model[name] = dict(prompt_tokens=p, completion_tokens=c)
        delta["per_model"] = per_model
        delta["cost_usd"] = round(sum(estimate_cost_usd(m, v["prompt_tokens"], v["completion_tokens"]) for m, v in per_model.items()), 6)
        return delta


_collector: UsageCollector | None = None
_collector_lock = threading.Lock()


def usage_collector() -> UsageCollector:
    """进程级单例"""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = UsageCollector()
    return _collector


def record_llm_usage(response: Any, *, model_name: str | None = None) -> None:
    """便捷入口：各调用点一行挂载，失败静默（不阻断主流程）"""
    try:
        usage_collector().record_response(response, model_name=model_name)
    except Exception:
        pass


def active_model_name() -> str:
    """当前生效的模型名：provider 覆盖（/model）→ 环境变量 → 兜底"""
    try:
        from mokioclaw.providers.openai_provider import get_active_model

        override = get_active_model()
        if override:
            return override
    except Exception:
        pass
    return os.getenv("MODEL", "") or os.getenv("OPENAI_MODEL", "") or _MODEL_NAME_FALLBACK
