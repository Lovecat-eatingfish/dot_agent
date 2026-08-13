import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { QueryResp } from "../types";
import { Button, Card, PageHeader, Spinner, ErrorBanner, Badge } from "../components/ui";

export default function QueryPage() {
  const [query, setQuery] = useState("");
  const [k, setK] = useState(5);
  const [rewrite, setRewrite] = useState(false);
  const [selfQuery, setSelfQuery] = useState(false);
  const [generateAnswer, setGenerateAnswer] = useState(false);
  const [useCache, setUseCache] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<QueryResp | null>(null);

  async function submit() {
    if (!query.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.query({
        query,
        k,
        rewrite,
        self_query: selfQuery,
        generate_answer: generateAnswer,
        use_cache: useCache,
      });
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="p-8 max-w-5xl">
      <PageHeader title="问答" subtitle="全链路 RAG：检索 → RRF 融合 → rerank → LIM 重排 → 答案生成 → 引用溯源 → guardrails" />

      {/* 查询输入 */}
      <Card className="p-5 mb-4">
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          rows={3}
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:border-brand-500 resize-y"
          placeholder="输入你的问题..."
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          }}
        />

        <div className="flex flex-wrap items-center gap-4 mt-3">
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={rewrite} onChange={(e) => setRewrite(e.target.checked)} className="accent-brand-600" />
            Query 改写
          </label>
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={selfQuery} onChange={(e) => setSelfQuery(e.target.checked)} className="accent-brand-600" />
            Self-Query
          </label>
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={generateAnswer} onChange={(e) => setGenerateAnswer(e.target.checked)} className="accent-brand-600" />
            生成答案
          </label>
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={useCache} onChange={(e) => setUseCache(e.target.checked)} className="accent-brand-600" />
            语义缓存
          </label>
          <div className="flex items-center gap-2 text-sm text-slate-600">
            <span>k:</span>
            <input
              type="number"
              min={1}
              max={20}
              value={k}
              onChange={(e) => setK(Math.max(1, Number(e.target.value)))}
              className="w-16 px-2 py-1 border border-slate-300 rounded text-sm"
            />
          </div>
          <Button onClick={submit} disabled={loading || !query.trim()} className="ml-auto">
            {loading ? <Spinner /> : "查询 (Ctrl+Enter)"}
          </Button>
        </div>
      </Card>

      {error && <div className="mb-4"><ErrorBanner message={error} /></div>}

      {result && (
        <div className="space-y-4">
          {/* 状态栏 */}
          <Card className="p-3 flex items-center gap-3 flex-wrap">
            {result.cached && <Badge color="green">缓存命中</Badge>}
            {result.degraded && result.degraded.length > 0 && (
              result.degraded.map((d, i) => <Badge key={i} color="red">降级: {d}</Badge>)
            )}
            {result.trace_id && (
              <Link to={`/trace?trace_id=${encodeURIComponent(result.trace_id)}`} className="ml-auto text-xs text-brand-600 hover:underline">
                trace: {result.trace_id} →
              </Link>
            )}
          </Card>

          {/* 答案 */}
          {result.answer && (
            <Card className="p-5">
              <h3 className="text-sm font-semibold text-slate-700 mb-2">答案</h3>
              <div className="text-sm text-slate-800 whitespace-pre-wrap leading-relaxed">{result.answer}</div>
            </Card>
          )}

          {/* 引用 */}
          {result.citations && result.citations.length > 0 && (
            <Card className="p-5">
              <h3 className="text-sm font-semibold text-slate-700 mb-2">引用来源（{result.citations.length}）</h3>
              <div className="flex flex-wrap gap-2">
                {result.citations.map((c, i) => (
                  <div key={i} className="text-xs bg-slate-50 rounded px-2 py-1 border border-slate-200">
                    <span className="text-brand-700 font-medium">[{c.index}]</span>
                    <span className="text-slate-600 ml-1">{c.doc_id}</span>
                    {c.source && <span className="text-slate-400 ml-1">({c.source})</span>}
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* 检索片段 */}
          {result.chunks && result.chunks.length > 0 && (
            <Card className="p-5">
              <h3 className="text-sm font-semibold text-slate-700 mb-3">检索片段（{result.chunks.length}）</h3>
              <div className="space-y-2">
                {result.chunks.map((c, i) => (
                  <div key={i} className="p-3 bg-slate-50 rounded border border-slate-200">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <Badge color="slate">#{i + 1}</Badge>
                      {c.metadata.heading_path && <Badge color="blue">{c.metadata.heading_path}</Badge>}
                      {c.metadata.doc_id && <span className="text-xs text-slate-400">{c.metadata.doc_id}</span>}
                      {typeof c.metadata.char_start === "number" && (
                        <span className="text-xs text-slate-400">
                          [{c.metadata.char_start}–{c.metadata.char_end}]
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-slate-600 font-mono whitespace-pre-wrap line-clamp-4">{c.content}</p>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {result.ok === false && <ErrorBanner message={result.error || "查询失败"} />}
        </div>
      )}
    </div>
  );
}
