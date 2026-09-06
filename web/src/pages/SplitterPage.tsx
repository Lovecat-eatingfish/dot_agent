import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getDocumentChunks, listDocuments, splitPreview } from "../api";
import type { DocumentRecord, SplitPreviewResp } from "../types";
import { Badge, Button, Card, ErrorBanner, EmptyState } from "../components/ui";
import { PageHeader } from "../App";
import FamilyList from "../components/FamilyList";

const DEFAULTS = { parent_size: 1200, parent_overlap: 100, child_size: 500, child_overlap: 50 };

function StatBar({ stats }: { stats: { parents: number; children: number; chars: number } }) {
  return (
    <div className="flex gap-2">
      <Badge tone="blue">父块 {stats.parents}</Badge>
      <Badge tone="amber">子块 {stats.children}</Badge>
      <Badge>约 {stats.chars} 字</Badge>
    </div>
  );
}

export default function SplitterPage() {
  const [searchParams] = useSearchParams();

  // ---------- 实际切片（已入库文档） ----------
  const [docs, setDocs] = useState<DocumentRecord[]>([]);
  const [activeDocId, setActiveDocId] = useState<string>("");
  const [actual, setActual] = useState<SplitPreviewResp | null>(null);
  const [actualLoading, setActualLoading] = useState(false);

  // ---------- 参数预览（试验台） ----------
  const [text, setText] = useState("");
  const [params, setParams] = useState(DEFAULTS);
  const [preview, setPreview] = useState<SplitPreviewResp | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState("");

  const loadActual = useCallback(async (docId: string) => {
    setActual(null);
    setError("");
    if (!docId) return;
    setActualLoading(true);
    try {
      setActual(await getDocumentChunks(docId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActualLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const { documents } = await listDocuments();
        setDocs(documents);
        // 支持 /split?doc=<id> 直达（文档管理页跳转过来）
        const want = searchParams.get("doc");
        const target = documents.find((d) => d.id === want) ?? documents[0];
        if (target) {
          setActiveDocId(target.id);
          loadActual(target.id);
        }
      } catch {
        /* 后端未启动等情况，静默 */
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handlePreview() {
    setPreviewLoading(true);
    setError("");
    try {
      setPreview(await splitPreview({ text, ...params }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewLoading(false);
    }
  }

  const numField = (label: string, key: keyof typeof DEFAULTS) => (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-slate-500">{label}</span>
      <input
        type="number"
        min={0}
        value={params[key]}
        onChange={(e) => setParams({ ...params, [key]: Number(e.target.value) })}
        className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm focus:border-blue-500 focus:outline-none"
      />
    </label>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="切片预览"
        desc="看清切分：文档入库时真实写入向量库的分块结果，以及调整参数后的切分效果预览。"
      />

      {/* 实际切片 */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-[13px] font-semibold uppercase tracking-wide text-slate-500">
            实际切片
          </h3>
          <select
            value={activeDocId}
            onChange={(e) => {
              setActiveDocId(e.target.value);
              loadActual(e.target.value);
            }}
            className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-[13px] text-slate-600 shadow-sm focus:border-indigo-500 focus:outline-none"
          >
            <option value="">选择文档…</option>
            {docs.map((d) => (
              <option key={d.id} value={d.id}>
                {d.filename}（v{d.version}，{d.chunk_count} 块）
              </option>
            ))}
          </select>
          {actualLoading && <span className="text-xs text-slate-400">加载中…</span>}
        </div>
        <p className="text-xs leading-relaxed text-slate-400">
          <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-indigo-500 align-middle" />
          父块＝送入 LLM 的上下文
          <span className="mx-2 inline-block h-2 w-2 rounded-sm bg-amber-400 align-middle" />
          子块＝向量检索的命中粒度
        </p>

        {actual && (
          <>
            <StatBar stats={actual.stats} />
            {actual.families.length === 0 ? (
              <EmptyState text="该文档没有切片数据" />
            ) : (
              <FamilyList families={actual.families} />
            )}
          </>
        )}
        {!actual && !actualLoading && (
          <Card>
            <EmptyState text="从上方选择一个文档，查看它的真实切分结果" hint="没有入库文档？先去「文档入库」上传" />
          </Card>
        )}
      </section>

      <hr className="border-slate-200" />

      {/* 参数预览试验台 */}
      <section className="space-y-3">
        <h3 className="text-[13px] font-semibold uppercase tracking-wide text-slate-500">
          参数试验台<span className="ml-2 font-normal normal-case text-slate-400">不入库</span>
        </h3>

        <Card>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴一段文本，预览父子分块结构…"
            rows={6}
            className="w-full resize-y rounded-lg border border-slate-200 bg-slate-50/50 px-3 py-2.5 text-[13px] leading-relaxed focus:border-indigo-500 focus:bg-white focus:outline-none"
          />
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
            {numField("父块大小", "parent_size")}
            {numField("父块重叠", "parent_overlap")}
            {numField("子块大小", "child_size")}
            {numField("子块重叠", "child_overlap")}
            <div className="ml-auto">
              <Button variant="primary" loading={previewLoading} disabled={!text.trim()} onClick={handlePreview}>
                预览分块
              </Button>
            </div>
          </div>
        </Card>

        {preview && (
          <>
            <StatBar stats={preview.stats} />
            {preview.families.length === 0 ? (
              <EmptyState text="没有生成任何分块，试试调大块大小或输入更长文本" />
            ) : (
              <FamilyList families={preview.families} />
            )}
          </>
        )}
      </section>

      {error && <ErrorBanner message={error} />}
    </div>
  );
}
