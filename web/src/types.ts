// 后端 API 类型（对齐 src/dot/rag/server.py）

export interface DocumentRecord {
  id: string;
  filename: string;
  path: string;
  version: number;
  chunk_count: number;
  status: "active" | "deleted";
  created_at: string;
  updated_at: string;
}

export interface IngestResult {
  id: string;
  filename: string;
  version: number;
  chunk_count: number;
}

export interface IngestError {
  filename: string;
  error: string;
}

export interface ChunkItem {
  source: string;
  score: number | null;
  text: string;        // 子块原文（检索命中粒度）
  parent_text: string; // 父块文本（实际送入 LLM 的上下文）
}

export interface SplitFamily {
  parent_id: string;
  parent_text: string;
  children: string[];
  source: string;
}

export interface SplitPreviewResp {
  families: SplitFamily[];
  stats: { parents: number; children: number; chars: number };
}

export interface TraceStep {
  stage: string;
  cost_ms: number;
  start_ms?: number;      // 相对查询开始的偏移（瀑布图用）
  detail: Record<string, unknown>;
  children?: TraceStep[]; // 子 span（层级链路）
}

export interface TraceDetail {
  trace_id: string;
  question: string;
  answer: string;
  steps: TraceStep[];
  top_k: number;
  chunk_count: number;
  status: "done" | "error";
  created_at: string;
}

export interface SessionRecord {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface MessageRecord {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  trace_id: string;
  sources: string[];
  created_at: string;
}

// SSE 事件流
export type RagEvent =
  | { type: "stage"; stage: string; cost_ms: number; chunks: ChunkItem[] }
  | { type: "delta"; text: string }
  | { type: "session"; session_id: string; title: string }
  | { type: "done"; trace_id: string; answer: string; sources: string[]; message_id?: string }
  | { type: "error"; message: string };
