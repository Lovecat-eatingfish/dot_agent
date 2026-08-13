import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DocRecord } from "../types";
import { Button, Card, PageHeader, Spinner, ErrorBanner, Badge, useConfirm } from "../components/ui";

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { confirm, node } = useConfirm();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api.listDocs();
      if (r.ok) setDocs(r.documents);
      else setError("加载失败");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleDelete(docId: string) {
    const ok = await confirm(`确认逻辑删除文档 ${docId}？此操作可逆（标记 deleted，不物理删）`);
    if (!ok) return;
    try {
      await api.deleteDoc(docId);
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="p-8 max-w-5xl">
      <PageHeader title="文档管理" subtitle="已入库文档列表，支持逻辑删除（版本控制 + 软删除）" />

      <div className="flex items-center justify-between mb-4">
        <div className="text-sm text-slate-500">共 {docs.length} 个文档</div>
        <Button variant="secondary" onClick={load} disabled={loading}>
          {loading ? <Spinner /> : "刷新"}
        </Button>
      </div>

      {error && <div className="mb-4"><ErrorBanner message={error} /></div>}

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr className="text-left text-slate-500">
              <th className="px-4 py-3 font-medium">doc_id</th>
              <th className="px-4 py-3 font-medium">source</th>
              <th className="px-4 py-3 font-medium text-center">chunks</th>
              <th className="px-4 py-3 font-medium text-center">version</th>
              <th className="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {docs.length === 0 && !loading && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-slate-400">暂无文档，去「文档入库」上传</td>
              </tr>
            )}
            {docs.map((d) => (
              <tr key={d.doc_id} className="hover:bg-slate-50">
                <td className="px-4 py-3 font-mono text-xs text-slate-700">{d.doc_id}</td>
                <td className="px-4 py-3 text-slate-600">{d.source || "—"}</td>
                <td className="px-4 py-3 text-center"><Badge color="blue">{d.chunk_count}</Badge></td>
                <td className="px-4 py-3 text-center"><Badge color={d.version > 1 ? "amber" : "slate"}>v{d.version}</Badge></td>
                <td className="px-4 py-3 text-right">
                  <Button variant="danger" onClick={() => handleDelete(d.doc_id)}>
                    删除
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {node}
    </div>
  );
}
