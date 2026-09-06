import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { deleteDocument, listDocuments } from "../api";
import type { DocumentRecord } from "../types";
import { Badge, Button, Card, EmptyState, ErrorBanner } from "../components/ui";
import { PageHeader } from "../App";

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocumentRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setDocs((await listDocuments()).documents);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleDelete(doc: DocumentRecord) {
    if (!window.confirm(`确认删除文档「${doc.filename}」（v${doc.version}）？将同时清除其向量数据。`)) return;
    setDeleting(doc.id);
    setError("");
    try {
      await deleteDocument(doc.id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <PageHeader title="文档管理" desc="已入库文档的版本与状态；删除为逻辑删除并同步清除向量数据。" />
        <Button onClick={refresh} loading={loading}>
          刷新
        </Button>
      </div>

      {error && <ErrorBanner message={error} />}

      <Card>
        {docs.length === 0 ? (
          <EmptyState text="还没有入库文档" hint="去「文档入库」页上传或扫描目录" />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left font-mono text-[11px] uppercase tracking-wider text-slate-400">
                <th className="py-2 pr-3 font-medium">文件名</th>
                <th className="py-2 pr-3 font-medium">版本</th>
                <th className="py-2 pr-3 font-medium">Chunk</th>
                <th className="py-2 pr-3 font-medium">入库时间</th>
                <th className="py-2 pr-3 font-medium">状态</th>
                <th className="py-2 pr-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.id} className="border-b border-slate-50 hover:bg-slate-50">
                  <td className="py-2 pr-3 font-medium text-slate-700">{d.filename}</td>
                  <td className="py-2 pr-3">
                    <Badge tone="blue">v{d.version}</Badge>
                  </td>
                  <td className="py-2 pr-3 text-slate-500">{d.chunk_count}</td>
                  <td className="py-2 pr-3 text-xs text-slate-400">{formatTime(d.created_at)}</td>
                  <td className="py-2 pr-3">
                    {d.status === "active" ? <Badge tone="green">active</Badge> : <Badge tone="slate">deleted</Badge>}
                  </td>
                  <td className="py-2 pr-3 text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      <Link to={`/split?doc=${d.id}`}>
                        <Button>查看切片</Button>
                      </Link>
                      <Button variant="danger" loading={deleting === d.id} onClick={() => handleDelete(d)}>
                        删除
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <p className="text-xs text-slate-400">
        删除为逻辑删除（登记表标记 + 清除向量数据）；同名文档重新入库时版本号 +1。
      </p>
    </div>
  );
}

function formatTime(iso: string): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
