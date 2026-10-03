"""
evals.runner — 通用 agent 评估驱动（矩阵：被测 agent × 任务）

逐 (agent, task) 组合执行：隔离 workspace → adapter.run → verify.py 判分 →
归一化轨迹指标 → results.json。任何满足 AgentAdapter 协议的 agent 都能接入。

用法：
  .venv/Scripts/python.exe evals/runner.py                             # dot_agent × 全部任务
  .venv/Scripts/python.exe evals/runner.py --agents dot_agent,fake     # 多 agent 对比
  .venv/Scripts/python.exe evals/runner.py --only 001,008              # 指定任务
  .venv/Scripts/python.exe evals/runner.py --judge --delay 30          # judge + 限流间隔
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVALS_ROOT.parent
TASKS_ROOT = EVALS_ROOT / "tasks"
RESULTS_ROOT = EVALS_ROOT / "results"
RUN_ROOT = EVALS_ROOT / ".run"

sys.path.insert(0, str(EVALS_ROOT))
from adapters import load_adapters  # noqa: E402
from metrics import EvalContext, MetricResult, build_metric_pipeline  # noqa: E402


def load_task_meta(task_dir: Path) -> dict:
    """读 task.yaml（极简 key: value 解析，避免引入 yaml 依赖差异）"""
    meta = {"category": "gen", "judge": False, "timeout_s": 480}
    meta_file = task_dir / "task.yaml"
    if meta_file.exists():
        for line in meta_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key in ("judge",):
                meta[key] = value.lower() in ("true", "yes", "1")
            elif key in ("timeout_s",):
                meta[key] = int(value)
            else:
                meta[key] = value
    return meta


def snapshot_root_files() -> set[str]:
    """记录仓库根顶层文件（检测 agent 把文件写到 workspace 外——沙箱逃逸探针）"""
    known = {"evals"}  # 运行目录本身的变化不算
    return {p for p in {f.name for f in REPO_ROOT.iterdir()} if p not in known}


def spawn_env(workspace: Path | None = None, run_dir: Path | None = None) -> dict:
    """verify/setup 子进程统一环境：.venv/Scripts 前插 PATH，附带 WORKSPACE/RUN_DIR"""
    import os

    env = dict(os.environ)
    venv_bin = REPO_ROOT / ".venv" / "Scripts"
    env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
    if workspace:
        env["WORKSPACE"] = str(workspace)
    if run_dir:
        env["RUN_DIR"] = str(run_dir)
    return env


def run_one_task(adapter, task_dir: Path, run_dir: Path, timeout_override: int | None,
                 with_judge: bool = False, judge_samples: int = 1) -> dict:
    """执行单个 (agent, task) 组合，返回结果记录"""
    meta = load_task_meta(task_dir)
    task_text = (task_dir / "task.md").read_text(encoding="utf-8").strip()

    ws = run_dir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if (task_dir / "workspace").exists():
        shutil.copytree(task_dir / "workspace", ws, dirs_exist_ok=True)

    # 任务自定义前置钩子（如红队任务放置 workspace 外的诱饵文件）
    setup = task_dir / "setup.py"
    if setup.exists():
        subprocess.run([sys.executable, str(setup)], cwd=str(task_dir),
                       env=spawn_env(ws, run_dir), capture_output=True, text=True, timeout=30)

    root_before = snapshot_root_files()
    result = adapter.run(task_text, ws, run_dir)
    root_after = snapshot_root_files()
    leaked_to_root = sorted(root_after - root_before - {RUN_ROOT.name, RESULTS_ROOT.name})

    # 判分：verify.py 接收 WORKSPACE / RUN_DIR / AGENT_EXIT_CODE 环境变量
    verify = task_dir / "verify.py"
    if verify.exists():
        env = spawn_env(ws, run_dir)
        env["AGENT_EXIT_CODE"] = str(result.exit_code)
        verify_proc = subprocess.run(
            [sys.executable, str(verify)], cwd=str(task_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120,
        )
        verified = verify_proc.returncode == 0
        verify_log = (verify_proc.stdout + verify_proc.stderr)[-2000:]
    else:
        verified, verify_log = result.exit_code == 0, "(no verify.py; pass = agent exit 0)"

    trajectory = None
    log_text = ""
    if result.log_path and result.log_path.exists():
        log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    try:
        trajectory = adapter.collect_trajectory(ws, log_text)  # 支持接收日志的适配器
    except TypeError:
        trajectory = adapter.collect_trajectory(ws)
    metrics = trajectory.to_dict() if trajectory else {}

    # 环境失败识别：agent 正常退出但零活动（LLM 限流静默失败等），与能力失败区分
    no_activity = metrics.get("llm_calls") in (0, None) and metrics.get("tool_calls") in (0, None)
    infra_error = bool(result.error) or (result.exit_code == 0 and no_activity and not verified)
    # 限流降级识别：有活动但被限流打断、没有最终回答（retries 高 + 无结论文本）
    degraded = ((metrics.get("retries") or 0) >= 3
                and metrics.get("has_final_text") is False
                and metrics.get("tool_calls") not in (0, None))

    # 指标管道：确定性层永远执行（零 LLM），LLM 层受 --judge × task.yaml judge 双开关控制
    ctx = EvalContext(
        agent=adapter.name, task=task_dir.name, category=meta["category"],
        task_text=task_text, task_meta=meta, workspace=ws, run_dir=run_dir,
        agent_exit=result.exit_code, verified=verified, verify_log=verify_log,
        trajectory=trajectory, log_text=log_text, leaked_to_root=leaked_to_root,
        judge_samples=judge_samples,
    )
    metric_results: list[dict] = []
    judge_legacy: dict | None = None
    loop_suspect = False
    for metric in build_metric_pipeline(with_judge=with_judge and bool(meta.get("judge"))):
        try:
            mr = metric.run(ctx)
        except Exception as exc:
            mr = MetricResult(name=metric.name, kind=metric.kind,
                              reason=f"metric error: {type(exc).__name__}: {exc}")
        metric_results.append(mr.to_dict())
        if metric.name == "efficiency":
            loop_suspect = bool(mr.breakdown.get("loop_suspect"))
        if metric.name == "five_dim_judge":
            judge_legacy = {**mr.breakdown, "score": mr.breakdown.get("score_1_to_5"),
                            "reason": mr.reason, "cost": mr.cost, "verbose_logs": mr.verbose_logs}

    record = {
        "agent": adapter.name,
        "task": task_dir.name, "category": meta["category"],
        "agent_exit": result.exit_code, "verified": verified,
        "infra_error": infra_error, "loop_suspect": loop_suspect,
        "degraded": degraded,
        "wall_seconds": result.wall_seconds,
        "leaked_to_root": leaked_to_root,
        "verify_log": verify_log,
        "metrics": metrics,
        "metric_results": metric_results,
        "run_dir": str(run_dir),
        "error": result.error,
    }
    if with_judge and meta.get("judge"):
        record["judge"] = judge_legacy or {"score": None, "reason": "judge 未产出结果"}
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="通用 agent 评估驱动")
    parser.add_argument("--agents", default="dot_agent",
                        help="被测 agent 列表，逗号分隔（dot_agent / fake / evals/agents/*.yaml 名字）")
    parser.add_argument("--only", help="逗号分隔的任务名过滤，如 001,008")
    parser.add_argument("--judge", action="store_true", help="对 judge:true 的任务运行 LLM 评分")
    parser.add_argument("--timeout", type=int, default=None, help="单任务超时秒数")
    parser.add_argument("--delay", type=float, default=20.0,
                        help="运行间隔秒数（避免触发 provider 限流），0 表示不等待")
    parser.add_argument("--runs", type=int, default=1,
                        help="每任务重复运行次数（>1 时报告输出一致性指标）")
    parser.add_argument("--judge-samples", type=int, default=1,
                        help="judge 多采样次数（>1 时取均值降方差，代价成倍增加）")
    args = parser.parse_args()

    task_dirs = sorted(d for d in TASKS_ROOT.iterdir() if d.is_dir() and (d / "task.md").exists())
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        task_dirs = [d for d in task_dirs
                     if any(d.name == w or d.name.startswith(w) for w in wanted)]
    if not task_dirs:
        print("no tasks found"); return 1
    try:
        adapters = load_adapters(args.agents, REPO_ROOT, timeout_s=args.timeout or 480)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[eval] {exc}"); return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = RESULTS_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    runs = [(a, t, i) for a in adapters for t in task_dirs for i in range(max(1, args.runs))]
    for idx, (adapter, task_dir, run_idx) in enumerate(runs):
        if idx > 0 and args.delay > 0:
            time.sleep(args.delay)
        suffix = f"-r{run_idx}" if max(1, args.runs) > 1 else ""
        run_dir = RUN_ROOT / f"{stamp}-{adapter.name}-{task_dir.name}{suffix}"
        print(f"[eval] {adapter.name} × {task_dir.name} ..." if not suffix else
              f"[eval] {adapter.name} × {task_dir.name} (run {run_idx + 1}) ...", flush=True)
        try:
            r = run_one_task(adapter, task_dir, run_dir, args.timeout,
                             with_judge=args.judge, judge_samples=args.judge_samples)
            r["run_idx"] = run_idx
        except Exception as exc:
            r = {"agent": adapter.name, "task": task_dir.name, "verified": False,
                 "agent_exit": -1, "infra_error": True,
                 "error": f"{type(exc).__name__}: {exc}", "metrics": {}, "run_idx": run_idx}
        results.append(r)
        flag = "PASS" if r["verified"] else "FAIL"
        extra = f" judge={r['judge']['score']}" if r.get("judge", {}).get("score") else ""
        print(f"[eval] {adapter.name} × {task_dir.name}: {flag} "
              f"({r.get('wall_seconds', '?')}s, tools={r['metrics'].get('tool_calls', '?')}){extra}",
              flush=True)

    (out_dir / "results.json").write_text(
        json.dumps({"stamp": stamp, "agents": [a.name for a in adapters], "results": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[eval] results -> {out_dir / 'results.json'}")
    return 0 if all(r["verified"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
