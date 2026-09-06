// API 客户端：JSON 接口 + SSE 流式问答
import type { ChunkItem, DocumentRecord, IngestError, IngestResult, MessageRecord, RagEvent, SessionRecord, SplitPreviewResp, TraceDetail } from "./types";

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    throw new Error(detail?.detail ?? `${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

function postBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// ---------- 文档管理 ----------

export function listDocuments(): Promise<{ documents: DocumentRecord[] }> {
  return jsonFetch("/api/documents");
}

export function uploadDocuments(files: File[]): Promise<{ ingested: IngestResult[]; errors: IngestError[] }> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return jsonFetch("/api/documents/upload", { method: "POST", body: form });
}

export function ingestText(text: string, filename: string): Promise<IngestResult> {
  return jsonFetch("/api/documents/text", postBody({ text, filename }));
}

export function ingestDir(dir?: string): Promise<{ ingested: IngestResult[] }> {
  return jsonFetch("/api/documents/ingest-dir", postBody({ dir: dir ?? null }));
}

export function deleteDocument(docId: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/api/documents/${docId}`, { method: "DELETE" });
}

// ---------- 切片预览 ----------

export function splitPreview(params: {
  text: string;
  parent_size: number;
  parent_overlap: number;
  child_size: number;
  child_overlap: number;
}): Promise<SplitPreviewResp> {
  return jsonFetch("/api/split/preview", postBody(params));
}

// 已入库文档的实际切片结果
export function getDocumentChunks(docId: string): Promise<SplitPreviewResp> {
  return jsonFetch(`/api/documents/${docId}/chunks`);
}

// ---------- 会话聊天 ----------

export function listSessions(): Promise<{ sessions: SessionRecord[] }> {
  return jsonFetch("/api/sessions");
}

export function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/api/sessions/${sessionId}`, { method: "DELETE" });
}

export function listMessages(sessionId: string): Promise<{ messages: MessageRecord[] }> {
  return jsonFetch(`/api/sessions/${sessionId}/messages`);
}

export async function streamChat(
  body: { question: string; session_id?: string; top_k?: number },
  onEvent: (event: RagEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/chat", { ...postBody(body), signal });
  if (!resp.ok || !resp.body) throw new Error(`${resp.status} ${resp.statusText}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as RagEvent);
      }
    }
  }
}

// ---------- 检索 ----------

export function search(body: { question: string; top_k?: number }): Promise<{ chunks: ChunkItem[] }> {
  return jsonFetch("/api/search", postBody(body));
}

// ---------- 流式问答（SSE） ----------

export async function streamQuery(
  body: { question: string; top_k?: number },
  onEvent: (event: RagEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/query", { ...postBody(body), signal });
  if (!resp.ok || !resp.body) throw new Error(`${resp.status} ${resp.statusText}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 以空行分隔事件
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as RagEvent);
      }
    }
  }
}

// ---------- trace ----------

export function listTraces(limit = 50): Promise<{ traces: TraceDetail[] }> {
  return jsonFetch(`/api/traces?limit=${limit}`);
}

export function getTrace(traceId: string): Promise<TraceDetail> {
  return jsonFetch(`/api/traces/${traceId}`);
}
