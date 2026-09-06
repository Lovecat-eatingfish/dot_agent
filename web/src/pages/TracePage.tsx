import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getTrace, listTraces } from "../api";
import type { TraceDetail, TraceStep } from "../types";
import { Badge, Button, Card, EmptyState, ErrorBanner } from "../components/ui";

export default function TracePage() {
  const [traces, setTraces] = useState<TraceDetail[]>([]);
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [searchParams] = useSearchParams();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setTraces((await listTraces()).traces);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 支持 /trace?id=xxx 直达单条（问答页跳转过来）
  useEffect(() => {
    const id = searchParams.get("id");
    if (id) openTrace(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  async function openTrace(id: string) {
    setError("");
    try {
      setSelected(await getTrace(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const totalCost = (t: TraceDetail) => t.steps.reduce((sum, s) => sum + s.cost_ms, 0);
  // 瀑布图时间轴总长：所有 span 的最大结束时间
  const totalMs = selected ? Math.max(1, ...flattenSpans(selected.steps).map((r) => r.step.start_ms! + r.step.cost_ms)) : 1;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold text-slate-800">链路追踪</h2>
        <Button onClick={refresh} loading={loading}>
          刷新
        </Button>
      </div>

      {error && <ErrorBanner message={error} />}

      {/* 列表 */}
      <Card title="查询记录">
        {traces.length === 0 ? (
          <EmptyState text="还没有查询记录，去问答页提问后这里会显示完整链路" />
        ) : (
          <table className="w-full text-sm">
            <thead>
                <tr className="border-b border-slate-200 text-left font-mono text-[11px] uppercase tracking-wider text-slate-400">
                  <th className="py-2 pr-3 font-medium">时间</th>
                  <th className="py-2 pr-3 font-medium">问题</th>
                  <th className="py-2 pr-3 font-medium">片段</th>
                  <th className="py-2 pr-3 font-medium">总耗时</th>
                  <th className="py-2 pr-3 font-medium">状态</th>
                </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                <tr
                  key={t.trace_id}
                  onClick={() => openTrace(t.trace_id)}
                  className={`cursor-pointer border-b border-slate-50 hover:bg-slate-50 ${
                    selected?.trace_id === t.trace_id ? "bg-blue-50/60" : ""
                  }`}
                >
                  <td className="py-2 pr-3 text-xs text-slate-400">{formatTime(t.created_at)}</td>
                  <td className="max-w-[240px] truncate py-2 pr-3 text-slate-700">{t.question}</td>
                  <td className="py-2 pr-3 text-slate-500">{t.chunk_count}</td>
                  <td className="py-2 pr-3 text-slate-500">{Math.round(totalCost(t))}ms</td>
                  <td className="py-2 pr-3">
                    {t.status === "done" ? <Badge tone="green">done</Badge> : <Badge tone="red">error</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* 详情 */}
      {selected && (
        <Card
          title={
            <span className="flex items-center gap-2">
              链路详情 <Badge>{selected.trace_id}</Badge>
            </span>
          }
        >
          <div className="space-y-4">
            <div>
              <p className="text-xs font-medium text-slate-400">问题</p>
              <p className="mt-1 text-sm text-slate-700">{selected.question}</p>
            </div>

            {/* span 树瀑布图 */}
            <div>
              <p className="font-mono text-[11px] uppercase tracking-wider text-slate-400">span 瀑布图</p>
              <div className="mt-2 space-y-1">
                {flattenSpans(selected.steps).map(({ step, depth }, i) => (
                  <div
                    key={i}
                    className="group flex items-center gap-3 rounded-md py-1 pr-1 text-xs hover:bg-slate-50"
                    style={{ paddingLeft: depth * 20 + 4 }}
                  >
                    <span
                      className={`w-32 shrink-0 truncate font-mono text-[11px] ${
                        depth === 0 ? "font-semibold text-slate-700" : "text-slate-500"
                      }`}
                    >
                      {depth > 0 && <span className="mr-1 text-slate-300">└</span>}
                      {step.stage}
                    </span>
                    <div
                      className="relative h-4 flex-1 overflow-hidden rounded bg-slate-100"
                      style={{
                        backgroundImage:
                          "repeating-linear-gradient(to right, rgba(148,163,184,0.28) 0 1px, transparent 1px 48px)",
                      }}
                    >
                      <div
                        className={`absolute top-0 h-full rounded ${SPAN_COLORS[Math.min(depth, SPAN_COLORS.length - 1)]}`}
                        style={{
                          left: `${((step.start_ms ?? 0) / totalMs) * 100}%`,
                          width: `${Math.max(1.5, (step.cost_ms / totalMs) * 100)}%`,
                        }}
                        title={`${step.stage}: +${Math.round(step.start_ms ?? 0)}ms 起，耗时 ${Math.round(step.cost_ms)}ms`}
                      />
                    </div>
                    <span className="w-20 shrink-0 text-right font-mono text-[11px] text-slate-500">
                      {Math.round(step.cost_ms)}ms
                    </span>
                  </div>
                ))}
              </div>
              <p className="mt-1.5 text-right font-mono text-[10px] text-slate-300">
                时间轴 0 — {Math.round(totalMs)}ms
              </p>
            </div>

            <div>
              <p className="text-xs font-medium text-slate-400">回答</p>
              <p className="mt-1 whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm leading-relaxed text-slate-700">
                {selected.answer || "（无）"}
              </p>
            </div>

            <div>
              <p className="text-xs font-medium text-slate-400">原始数据</p>
              <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">
                {JSON.stringify(selected, null, 2)}
              </pre>
            </div>

            <div className="text-right">
              <Button onClick={() => setSelected(null)}>关闭详情</Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

// span 树 → 扁平行（保留深度用于缩进）；兼容旧格式（无 children 的平铺 steps）
interface SpanRow {
  step: TraceStep;
  depth: number;
}

function flattenSpans(steps: TraceStep[], depth = 0, out: SpanRow[] = []): SpanRow[] {
  for (const s of steps) {
    out.push({ step: s, depth });
    if (s.children?.length) flattenSpans(s.children, depth + 1, out);
  }
  return out;
}

const SPAN_COLORS = ["bg-slate-700/80", "bg-blue-500/80", "bg-emerald-500/80", "bg-amber-500/80"];
