import { useRef, useState } from "react";
import { ingestDir, ingestText, uploadDocuments } from "../api";
import type { IngestError, IngestResult } from "../types";
import { Badge, Button, Card, ErrorBanner } from "../components/ui";
import { PageHeader } from "../App";

export default function IngestPage() {
  const [uploading, setUploading] = useState(false);
  const [results, setResults] = useState<IngestResult[]>([]);
  const [errors, setErrors] = useState<IngestError[]>([]);
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);

  // 粘贴文本
  const [text, setText] = useState("");
  const [filename, setFilename] = useState("pasted_text.md");

  const fileInput = useRef<HTMLInputElement>(null);

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      const resp = await uploadDocuments(Array.from(files));
      setResults(resp.ingested);
      setErrors(resp.errors);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }

  async function handlePasteText() {
    setError("");
    setErrors([]);
    setResults([]);
    try {
      const result = await ingestText(text, filename || "pasted_text.md");
      setResults([result]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleIngestDir() {
    setUploading(true);
    setError("");
    setResults([]);
    setErrors([]);
    try {
      const resp = await ingestDir();
      setResults(resp.ingested);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="文档入库"
        desc="三种接入方式：拖拽上传文件、直接粘贴文本、扫描服务端文档目录。同名文档重新入库自动版本 +1。"
      />

      {error && <ErrorBanner message={error} />}

      {/* 文件上传 */}
      <Card title="上传文件">
        <div
          className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed py-10 transition ${
            dragOver ? "border-indigo-400 bg-indigo-50/60" : "border-slate-300 hover:border-indigo-300 hover:bg-slate-50"
          }`}
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            handleFiles(e.dataTransfer.files);
          }}
        >
          <p className="text-sm text-slate-500">点击选择或拖拽文件到此处</p>
          <p className="mt-1 font-mono text-[11px] text-slate-400">.txt / .md / .pdf / .docx</p>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".txt,.md,.pdf,.docx"
            className="hidden"
            onChange={(e) => handleFiles(e.target.files)}
          />
        </div>
        <div className="mt-3 text-right">
          <Button variant="primary" loading={uploading} onClick={handleIngestDir}>
            扫描服务端目录入库（src/dot/rag/docs）
          </Button>
        </div>
      </Card>

      {/* 粘贴文本 */}
      <Card title="粘贴文本">
        <input
          value={filename}
          onChange={(e) => setFilename(e.target.value)}
          placeholder="文档名（如 产品手册.md）"
          className="mb-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
        />
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="粘贴要入库的文本内容…"
          rows={6}
          className="w-full resize-y rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
        />
        <div className="mt-2 text-right">
          <Button variant="primary" disabled={!text.trim()} onClick={handlePasteText}>
            入库
          </Button>
        </div>
      </Card>

      {/* 结果 */}
      {(results.length > 0 || errors.length > 0) && (
        <Card title="入库结果">
          <div className="space-y-2">
            {results.map((r) => (
              <div key={r.id} className="flex items-center gap-2 text-sm">
                <Badge tone="green">v{r.version}</Badge>
                <span className="font-medium text-slate-700">{r.filename}</span>
                <span className="text-slate-400">{r.chunk_count} 个 chunk</span>
              </div>
            ))}
            {errors.map((e, i) => (
              <div key={i} className="text-sm text-red-600">
                {e.filename}: {e.error}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
