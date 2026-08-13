import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { IngestResp } from "../types";
import { Button, Card, PageHeader, Spinner, ErrorBanner, Badge } from "../components/ui";

type Tab = "file" | "text" | "url";

export default function IngestPage() {
  const [tab, setTab] = useState<Tab>("file");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<IngestResp | null>(null);

  // 文本/URL 表单
  const [text, setText] = useState("");
  const [source, setSource] = useState("text");
  const [url, setUrl] = useState("");

  // 文件
  const [fileName, setFileName] = useState("");

  async function submitText() {
    if (!text.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.ingestText(text, source);
      setResult(r);
      setText("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function submitUrl() {
    if (!url.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.ingestUrl(url);
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function submitFile(file: File) {
    setFileName(file.name);
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.ingestFile(file);
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: "file", label: "上传文件" },
    { key: "text", label: "文本内容" },
    { key: "url", label: "URL 网页" },
  ];

  return (
    <div className="p-8 max-w-3xl">
      <PageHeader title="文档入库" subtitle="上传文件 / 粘贴文本 / 抓取 URL，解析后结构感知分块入库" />

      <div className="flex gap-1 mb-4 border-b border-slate-200">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key
                ? "border-brand-600 text-brand-700"
                : "border-transparent text-slate-500 hover:text-slate-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <Card className="p-5">
        {tab === "file" && (
          <div>
            <label className="block">
              <div className="border-2 border-dashed border-slate-300 rounded-lg p-8 text-center cursor-pointer hover:border-brand-400 transition-colors">
                <input
                  type="file"
                  className="hidden"
                  accept=".md,.txt,.pdf"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) submitFile(f);
                  }}
                />
                {fileName ? (
                  <div className="text-sm text-slate-700">
                    <Badge color="blue">{fileName}</Badge>
                  </div>
                ) : (
                  <div className="text-sm text-slate-500">
                    点击或拖拽文件到此处上传
                    <div className="text-xs text-slate-400 mt-1">支持 .md / .txt / .pdf</div>
                  </div>
                )}
              </div>
            </label>
          </div>
        )}

        {tab === "text" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-slate-600 mb-1">来源标识</label>
              <input
                value={source}
                onChange={(e) => setSource(e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:border-brand-500"
                placeholder="text"
              />
            </div>
            <div>
              <label className="block text-sm text-slate-600 mb-1">文本内容</label>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={10}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm font-mono focus:outline-none focus:border-brand-500 resize-y"
                placeholder="粘贴文本内容..."
              />
            </div>
            <Button onClick={submitText} disabled={loading || !text.trim()}>
              {loading ? <Spinner /> : "入库"}
            </Button>
          </div>
        )}

        {tab === "url" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-slate-600 mb-1">URL 地址</label>
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:border-brand-500"
                placeholder="https://example.com/article"
              />
            </div>
            <Button onClick={submitUrl} disabled={loading || !url.trim()}>
              {loading ? <Spinner /> : "抓取并入库"}
            </Button>
          </div>
        )}
      </Card>

      {loading && (
        <div className="mt-4 flex items-center gap-2 text-sm text-slate-500">
          <Spinner /> 处理中...
        </div>
      )}

      {error && <div className="mt-4"><ErrorBanner message={error} /></div>}

      {result && (
        <Card className="mt-4 p-5">
          {result.ok ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Badge color="green">成功</Badge>
                <span className="text-sm text-slate-700">入库完成</span>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-slate-400">doc_id:</span>
                  <code className="ml-2 text-slate-700">{result.doc_id}</code>
                </div>
                <div>
                  <span className="text-slate-400">chunks:</span>
                  <span className="ml-2 font-medium text-brand-700">{result.chunks}</span>
                </div>
                {result.source && (
                  <div>
                    <span className="text-slate-400">source:</span>
                    <span className="ml-2 text-slate-700">{result.source}</span>
                  </div>
                )}
              </div>
              <div className="flex gap-3 pt-2 text-sm">
                <Link to="/split" className="text-brand-600 hover:underline">去切片预览 →</Link>
                <Link to="/query" className="text-brand-600 hover:underline">去问答 →</Link>
              </div>
            </div>
          ) : (
            <ErrorBanner message={result.error || "入库失败"} />
          )}
        </Card>
      )}
    </div>
  );
}
