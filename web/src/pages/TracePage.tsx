import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api } from "../api";
import type { TraceResp, TraceStep } from "../types";
import { Button, Card, PageHeader, Spinner, ErrorBanner, Badge } from "../components/ui";

export default function TracePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [traceId, setTraceId] = useState(searchParams.get("trace_id") || "");
  const [result, setResult] = useState<TraceResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function lookup(id: string) {
    if (!id.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.getTrace(id.trim());
      setResult(r);
      setSearchParams({ trace_id: id.trim() });
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  // 带参跳转自动查询
  useEffect(() => {
    const tid = searchParams.get("trace_id");
    if (tid) {
      setTraceId(tid);
      lookup(tid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const trace = result?.trace;

  return (
    <div className="p-8 max-w-4xl">
      <PageHeader title="链路追踪" subtitle="按 trace_id 查看单次请求的完整链路：每步召回/分数/耗时/降级" />

      <Card className="p-4 mb-4">
        <div className="flex gap-2">
          <input
            value={traceId}
            onChange={(e) => setTraceId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && lookup(traceId)}
            className="flex-1 px-3 py-2 border border-slate-300 rounded-md text-sm font-mono focus:outline-none focus:border-brand-500"
            placeholder="输入 trace_id，如 rag-trace-20260814-103045-a1b2c3"
          />
          <Button onClick={() => lookup(traceId)} disabled={loading || !traceId.trim()}>
            {loading ? <Spinner /> : "查询"}
          </Button>
        </div>
      </Card>

      {error && <div className="mb-4"><ErrorBanner message={error} /></div>}

      {result && !result.ok && <ErrorBanner message={result.error || "未找到 trace"} />}

      {trace && (
        <div className="space-y-4">
          {/* 概要 */}
          <Card className="p-5">
            <div className="grid grid-cols-3 gap-4">
              <div>
                <div className="text-xs text-slate-400">trace_id</div>
                <div className="text-sm font-mono text-slate-700 truncate" title={trace.trace_id}>{trace.trace_id}</div>
              </div>
              <div>
                <div className="text-xs text-slate-400">查询</div>
                <div className="text-sm text-slate-700 truncate" title={trace.query}>{trace.query || "—"}</div>
              </div>
              <div>
                <div className="text-xs text-slate-400">时间</div>
                <div className="text-sm text-slate-700">{trace.created_at}</div>
              </div>
            </div>
            {trace.degraded.length > 0 && (
              <div className="mt-3 flex items-center gap-2 flex-wrap">
                <span className="text-xs text-slate-400">降级标记:</span>
                {trace.degraded.map((d, i) => <Badge key={i} color="red">{d}</Badge>)}
              </div>
            )}
          </Card>

          {/* 步骤耗时图（若有 ms 字段）*/}
          {trace.steps.some((s) => typeof s.ms === "number") && (
            <Card className="p-5">
              <h3 className="text-sm font-semibold text-slate-700 mb-3">各步骤耗时</h3>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={trace.steps.map((s) => ({ step: s.step, ms: Number(s.ms) || 0 }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="step" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="ms" fill="#2563eb" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* 时间线 */}
          <Card className="p-5">
            <h3 className="text-sm font-semibold text-slate-700 mb-4">步骤时间线（{trace.steps.length} 步）</h3>
            <div className="relative">
              {trace.steps.map((step, i) => (
                <StepNode key={i} step={step} isLast={i === trace.steps.length - 1} />
              ))}
            </div>
          </Card>
        </div>
      )}

      {!trace && !loading && !error && (
        <Card className="p-8 text-center text-sm text-slate-400">
          输入 trace_id 查询链路，或从问答页点击 trace 链接跳转
        </Card>
      )}
    </div>
  );
}

function StepNode({ step, isLast }: { step: TraceStep; isLast: boolean }) {
  const extraKeys = Object.keys(step).filter(
    (k) => !["step", "ts", "degraded"].includes(k),
  );
  return (
    <div className="flex gap-3 pb-6 relative">
      {/* 轴线 */}
      {!isLast && <div className="absolute left-[7px] top-4 bottom-0 w-px bg-slate-200" />}
      <div className="w-3.5 h-3.5 rounded-full bg-brand-500 mt-1.5 shrink-0 ring-4 ring-brand-50" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-slate-800">{step.step}</span>
          {typeof step.hits === "number" && <Badge color="blue">hits: {step.hits}</Badge>}
          {typeof step.refs === "number" && <Badge color="green">refs: {step.refs}</Badge>}
          {typeof step.ms === "number" && <Badge color="amber">{step.ms}ms</Badge>}
        </div>
        {/* 额外字段 */}
        {extraKeys.length > 0 && (
          <div className="mt-1 text-xs text-slate-500 space-y-0.5">
            {extraKeys.map((k) => (
              <div key={k} className="font-mono break-all">
                <span className="text-slate-400">{k}:</span>{" "}
                {formatValue(step[k])}
              </div>
            ))}
          </div>
        )}
        <div className="text-xs text-slate-400 mt-0.5">{step.ts}</div>
      </div>
    </div>
  );
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
