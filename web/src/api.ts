// fetch 封装：开发时 baseURL=/api（走 vite proxy 到 8000），生产同源
const BASE = import.meta.env.DEV ? "/api" : "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { Accept: "application/json" },
    ...init,
  });
  if (!resp.ok && resp.status !== 422) {
    // 422 由调用方按 json 处理（FastAPI 校验错误体里有 detail）
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

import type {
  HealthResp,
  IngestResp,
  QueryReq,
  QueryResp,
  SplitPreviewResp,
  DocumentsResp,
  TraceResp,
  ApiOk,
} from "./types";

export const api = {
  health: () => req<HealthResp>("/health"),

  ingestText: (content: string, source = "text", docId?: string) =>
    req<IngestResp>("/ingest/text", jsonBody({ content, source, doc_id: docId })),

  ingestUrl: (url: string, docId?: string) =>
    req<IngestResp>("/ingest/url", jsonBody({ url, doc_id: docId })),

  ingestFile: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return req<IngestResp>("/ingest/file", { method: "POST", body: form });
  },

  previewSplit: (
    content: string,
    opts: { parentSize: number; childSize: number; childOverlap: number; source?: string },
  ) =>
    req<SplitPreviewResp>(
      "/preview/split",
      jsonBody({
        content,
        source: opts.source ?? "preview",
        parent_size: opts.parentSize,
        child_size: opts.childSize,
        child_overlap: opts.childOverlap,
      }),
    ),

  query: (q: QueryReq) => req<QueryResp>("/query", jsonBody(q)),

  listDocs: () => req<DocumentsResp>("/documents"),

  deleteDoc: (docId: string) =>
    req<ApiOk & { doc_id: string }>(`/documents/${encodeURIComponent(docId)}`, {
      method: "DELETE",
    }),

  getTrace: (traceId: string) => req<TraceResp>(`/trace/${encodeURIComponent(traceId)}`),

  clearCache: () => req<ApiOk & { cleared?: number; error?: string }>("/cache/clear", { method: "POST" }),
};
