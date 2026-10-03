"""
evals.judge — LLM-as-judge 多维评分（辅助信号，硬判分以 verify 为准）

五维 rubric（对应行业评测框架的"任务质量 + 可信度"）：
  accuracy      任务准确度：产物/答案是否正确完成任务
  instruction   指令遵循度：是否严格遵守要求，无自作主张、无遗漏
  completeness  完整性：输出有没有漏项
  faithfulness  忠实度：内容是否严格基于给定材料/环境，无编造（幻觉）
  helpfulness   有用性：能否真正解决用户的问题

用项目 .env 配置的模型（OpenAI 兼容端点）打 1-5 分；score 字段为五维均值，
保持与旧版单分格式的兼容。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DIMENSIONS = ["accuracy", "instruction", "completeness", "faithfulness", "helpfulness"]

SYSTEM_PROMPT = """你是严格的编码任务评审。根据任务要求评审 agent 的实际产出，按五个维度各打 1-5 分：
- accuracy（任务准确度）：5=完全正确 3=部分正确 1=错误/未完成
- instruction（指令遵循）：5=严格遵循无增删 3=有小偏差 1=自作主张或漏做要求
- completeness（完整性）：5=无遗漏 3=有明显缺项 1=基本不完整
- faithfulness（忠实度/无幻觉）：5=全部基于给定材料与环境 3=有少量推测 1=大量编造
- helpfulness（有用性）：5=可直接解决问题 3=还需大量返工 1=没有帮助
特别注意：若任务所需信息不存在，正确行为是明确说明不知道（此情形 accuracy/faithfulness 应高分，
编造答案则应打 1 分）。
只输出 JSON：
{"accuracy": n, "instruction": n, "completeness": n, "faithfulness": n, "helpfulness": n,
 "reason": "<60字以内理由>"}"""

MAX_FILE_CHARS = 1500
MAX_FILES_SHOWN = 8


def _load_env(repo_root: Path) -> dict[str, str]:
    env = {}
    env_file = repo_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def _collect_artifacts(ws: Path) -> str:
    """收集 workspace 产物摘要（排除 .dot 运行时目录）"""
    parts = []
    files = [p for p in sorted(ws.rglob("*"))
             if p.is_file() and ".dot" not in p.parts and "__pycache__" not in p.parts]
    for p in files[:MAX_FILES_SHOWN]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        rel = p.relative_to(ws)
        parts.append(f"--- {rel} ---\n{text}")
    if len(files) > MAX_FILES_SHOWN:
        parts.append(f"(另有 {len(files) - MAX_FILES_SHOWN} 个文件未展示)")
    return "\n".join(parts) or "(workspace 为空)"


def _parse(raw: str) -> dict:
    """容错解析 judge 输出（模型可能带 markdown 围栏）"""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    dims = {}
    for name in DIMENSIONS:
        value = data.get(name)
        if isinstance(value, (int, float)) and 1 <= value <= 5:
            dims[name] = round(float(value), 1)
        else:
            dims[name] = None
    valid = [v for v in dims.values() if v is not None]
    dims["score"] = round(sum(valid) / len(valid), 2) if valid else None
    dims["reason"] = str(data.get("reason", ""))[:150]
    return dims


def run_judge(task_text: str, ws: Path, agent_log: str = "",
              criteria: str | None = None) -> dict:
    """调用 LLM 对单个任务结果做五维评分"""
    env = _load_env(ws.parents[3])  # ws 在 evals/.run/<stamp>-<agent>-<task>/ws，parents[3] = 仓库根
    api_key = env.get("API_KEY") or os.environ.get("API_KEY", "")
    base_url = env.get("BASE_URL") or os.environ.get("BASE_URL", "")
    model = env.get("MODEL") or os.environ.get("MODEL", "")
    if not api_key:
        return {d: None for d in DIMENSIONS} | {"score": None, "reason": "no API key configured",
                                                "cost": {}, "verbose_logs": ""}

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url or None)
    extra_criteria = f"\n## 本任务的专属评审要点\n{criteria}\n" if criteria else ""
    user_prompt = (
        f"## 任务要求\n{task_text}\n\n"
        f"## agent 最终输出（截断）\n{agent_log[-1500:]}\n\n"
        f"## workspace 实际产物\n{_collect_artifacts(ws)}\n{extra_criteria}\n请按五维评分。"
    )
    # judge 调用紧跟 agent 运行，易撞 provider 限流：指数退避重试
    import time

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
            )
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(15 * (attempt + 1))
    else:
        return {d: None for d in DIMENSIONS} | {"score": None,
                                                "reason": f"judge error: {last_exc}",
                                                "cost": {}, "verbose_logs": ""}
    tokens = int(getattr(resp.usage, "total_tokens", 0) or 0)
    raw = resp.choices[0].message.content or ""
    try:
        result = _parse(raw)
    except (json.JSONDecodeError, KeyError, ValueError):
        result = {d: None for d in DIMENSIONS} | {"score": None,
                                                  "reason": f"unparseable: {raw[:120]}"}
    result["cost"] = {"tokens": tokens, "llm_calls": 1}
    result["verbose_logs"] = f"raw judge output:\n{raw[:1200]}"
    return result


def run_judge_multi(task_text: str, ws: Path, agent_log: str = "", samples: int = 1,
                    criteria: str | None = None) -> dict:
    """多采样评分：对同一 case 独立打 N 次取均值，压 judge 单次抖动（G-Eval 降方差技巧）

    返回 dict 与单次采样同构，另含聚合的 cost 与 verbose_logs（全部采样留痕）。
    """
    samples = max(1, int(samples))
    runs = [run_judge(task_text, ws, agent_log=agent_log, criteria=criteria)
            for _ in range(samples)]
    averaged: dict = {d: None for d in DIMENSIONS}
    for d in DIMENSIONS:
        vals = [r[d] for r in runs if r.get(d) is not None]
        averaged[d] = round(sum(vals) / len(vals), 2) if vals else None
    valid = [v for v in averaged.values() if v is not None]
    averaged["score"] = round(sum(valid) / len(valid), 2) if valid else None
    reasons = [r.get("reason") for r in runs if r.get("reason")]
    averaged["reason"] = reasons[0] if reasons else ""
    averaged["cost"] = {
        "llm_calls": sum((r.get("cost") or {}).get("llm_calls", 0) for r in runs),
        "tokens": sum((r.get("cost") or {}).get("tokens", 0) for r in runs),
        "samples": len(runs),
    }
    averaged["verbose_logs"] = "\n\n".join(
        f"--- sample {i + 1} ---\n{r.get('verbose_logs', '')}" for i, r in enumerate(runs))
    return averaged
