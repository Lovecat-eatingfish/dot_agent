"""
evals.report — 汇总 results.json 生成 markdown 报告（支持跨 agent 对比矩阵）

用法：
  .venv/Scripts/python.exe evals/report.py evals/results/<stamp>/results.json
输出：同目录 report.md + stdout 摘要
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

CATEGORY_LABEL = {"gen": "生成", "fix": "修复", "redteam": "红队", "trust": "可信度", "robust": "鲁棒"}
JUDGE_DIM_LABEL = {"accuracy": "准确度", "instruction": "指令遵循", "completeness": "完整性",
                   "faithfulness": "忠实度", "helpfulness": "有用性"}


def _cell(r: dict | None) -> str:
    """矩阵单元格：✅ 通过 / ⚠️ 通过但有安全标记 / ❌ 失败 / — 未运行"""
    if r is None:
        return "—"
    if not r.get("verified"):
        return "❌"
    m = r.get("metrics") or {}
    sec = m.get("security") or {}
    flagged = bool(r.get("leaked_to_root") or sec.get("leaked_to_root")
                   or sec.get("out_of_workspace_args"))
    judge = (r.get("judge") or {}).get("score")
    mark = "⚠️" if flagged else "✅"
    return f"{mark}({judge})" if judge else mark


def build_report(results_path: Path) -> tuple[str, dict]:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    results = data["results"]
    agents = data.get("agents") or sorted({r.get("agent", "dot_agent") for r in results})

    # 跨 agent 对比矩阵（行=任务，列=agent）
    by_pair: dict[tuple[str, str], dict] = {}
    task_order: list[str] = []
    for r in results:
        pair = (r["task"], r.get("agent", "dot_agent"))
        by_pair[pair] = r
        if r["task"] not in task_order:
            task_order.append(r["task"])

    lines = [
        "# Agent 评估报告",
        "",
        f"- 运行批次：`{data['stamp']}`　被测 agent：{'、'.join(agents)}",
        "",
        "## 对比矩阵",
        "",
        "| 任务 | " + " | ".join(f"{a}" for a in agents) + " |",
        "| --- | " + " | ".join("---" for _ in agents) + " |",
    ]
    for task in task_order:
        cells = [_cell(by_pair.get((task, a))) for a in agents]
        lines.append(f"| {task} | " + " | ".join(cells) + " |")

    # 各 agent 汇总（含环境失败与限流降级区分）
    lines += ["", "## 各 agent 汇总", "",
              "| agent | 通过率 | 环境失败 | 限流降级 | judge 均分 | 平均工具调用 | P95耗时 | 平均耗时 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    summary = {}
    for a in agents:
        rows = [r for r in results if r.get("agent", "dot_agent") == a]
        passed = sum(1 for r in rows if r["verified"])
        infra = sum(1 for r in rows if r.get("infra_error"))
        degraded = sum(1 for r in rows if r.get("degraded"))
        scores = [r["judge"]["score"] for r in rows if (r.get("judge") or {}).get("score")]
        tools = [r["metrics"]["tool_calls"] for r in rows
                 if (r.get("metrics") or {}).get("tool_calls") is not None]
        walls = sorted(r["wall_seconds"] for r in rows if r.get("wall_seconds") is not None)
        p95 = walls[min(len(walls) - 1, int(len(walls) * 0.95))] if walls else None
        avg_wall = round(sum(walls) / len(walls), 1) if walls else None
        avg_score = round(sum(scores) / len(scores), 2) if scores else None
        avg_tools = round(sum(tools) / len(tools), 1) if tools else None
        summary[a] = {"passed": passed, "total": len(rows)}
        lines.append(
            f"| {a} | {passed}/{len(rows)} | {infra} | {degraded} "
            f"| {avg_score if avg_score is not None else '—'} "
            f"| {avg_tools if avg_tools is not None else '—'} "
            f"| {f'{p95}s' if p95 is not None else '—'} "
            f"| {f'{avg_wall}s' if avg_wall is not None else '—'} |")

    # ---- 五大维度汇总（面试口径：准不准 / 信不信 / 快不贵 / 够安全 / 用户认不认） ----
    lines += ["", "## 五大维度汇总", ""]
    for a in agents:
        rows = [r for r in results if r.get("agent", "dot_agent") == a]
        judged = [r["judge"] for r in rows if r.get("judge")]

        def _dim(name: str):
            vals = [j[name] for j in judged if j.get(name) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        def _fmt(v):
            return f"{v}" if v is not None else "—"

        passed = sum(1 for r in rows if r["verified"])
        redteam = [r for r in rows if r.get("category") == "redteam"]
        redteam_pass = sum(1 for r in redteam if r["verified"])
        leaks = sum(1 for r in rows if r.get("leaked_to_root")
                    or (r.get("metrics") or {}).get("security", {}).get("out_of_workspace_args"))
        loops = sum(1 for r in rows if r.get("loop_suspect"))
        retries = [r["metrics"]["retries"] for r in rows
                   if (r.get("metrics") or {}).get("retries") is not None]
        tokens = [r["metrics"]["tokens"] for r in rows
                  if (r.get("metrics") or {}).get("tokens") is not None]

        lines += [f"### {a}", ""]
        lines += [
            f"- **任务质量（准不准）**：verify 通过率 {passed}/{len(rows)}"
            f"；judge 均分 准确度 {_fmt(_dim('accuracy'))} / 指令遵循 {_fmt(_dim('instruction'))}"
            f" / 完整性 {_fmt(_dim('completeness'))} / 有用性 {_fmt(_dim('helpfulness'))}",
            f"- **可信度（信不信）**：judge 忠实度 {_fmt(_dim('faithfulness'))}"
            f"；越界/泄漏标记 {leaks} 次；死循环嫌疑 {loops} 次",
            f"- **性能成本（快不贵）**：平均工具步数 {_fmt(round(sum(tools) / len(tools), 1) if tools else None)}"
            f"；平均重试 {_fmt(round(sum(retries) / len(retries), 1) if retries else None)}"
            f"；token（未记录则为 —）{_fmt(round(sum(tokens) / len(tokens)) if tokens else None)}",
            f"- **安全（够安全）**：红队任务通过 {redteam_pass}/{len(redteam)}"
            f"（提示注入 / 路径穿越 / 隐私泄漏 / 韧性专项见矩阵）",
            "- **业务（用户认不认）**：线上观测指标（满意度/接管率/留存），离线平台不覆盖",
            "",
        ]

    # ---- 一致性（--runs >1 时） ----
    multi = defaultdict(list)
    for r in results:
        multi[(r.get("agent", "dot_agent"), r["task"])].append(bool(r["verified"]))
    repeated = {k: v for k, v in multi.items() if len(v) > 1}
    if repeated:
        lines += ["## 一致性（多次运行）", ""]
        for (agent, task), flags in sorted(repeated.items()):
            stable = "一致" if len(set(flags)) == 1 else "**不一致**"
            lines.append(f"- {agent} × {task}：{sum(flags)}/{len(flags)} 通过，结果{stable}")
        lines.append("")

    # 分类通过率（按 agent × 类别）
    lines += ["", "## 分类通过率", ""]
    by_cat: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in results:
        by_cat[(r.get("agent", "dot_agent"), r.get("category", "?"))].append(bool(r["verified"]))
    for (agent, cat), flags in sorted(by_cat.items()):
        label = CATEGORY_LABEL.get(cat, cat)
        lines.append(f"- {agent} / {label}：{sum(flags)}/{len(flags)}")

    # 失败归因
    failures = [r for r in results if not r["verified"]]
    if failures:
        lines += ["", "## 失败归因", ""]
        for r in failures:
            lines += [f"### {r.get('agent', '?')} × {r['task']}", "", "```",
                      (r.get("verify_log") or r.get("error", ""))[-800:], "```", ""]

    return "\n".join(lines), summary


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python evals/report.py <results.json>"); return 1
    results_path = Path(sys.argv[1])
    report, _ = build_report(results_path)
    out = results_path.parent / "report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"[report] -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
