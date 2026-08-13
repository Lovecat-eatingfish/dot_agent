// 后端 API 响应类型（对齐 src/mokioclaw/rag/service.py）

export interface ApiOk {
  ok: boolean;
}

export interface HealthResp extends ApiOk {
  persist_dir: string;
}

export interface IngestResp extends ApiOk {
  doc_id: string;
  chunks: number;
  source?: string;
  error?: string;
}

export interface ChunkMeta {
  source?: string;
  doc_id?: string;
  chunk_index?: number;
  heading_path?: string;
  page?: number;
  char_start?: number;
  char_end?: number;
  parent_index?: number;
  [k: string]: unknown;
}

export interface ChunkItem {
  content: string;
  metadata: ChunkMeta;
}

export interface Citation {
  index: number;
  doc_id: string;
  source: string;
}

export interface QueryResp extends ApiOk {
  query?: string;
  answer?: string;
  trace_id?: string;
  cached?: boolean;
  degraded?: string[];
  citations?: Citation[];
  chunks?: ChunkItem[];
  error?: string;
}

export interface SplitPreviewResp extends ApiOk {
  parents: ChunkItem[];
  children: ChunkItem[];
  stats: {
    parent_count: number;
    child_count: number;
    avg_child_len: number;
  };
  error?: string;
}

export interface DocRecord {
  doc_id: string;
  source: string;
  chunk_count: number;
  version: number;
}

export interface DocumentsResp extends ApiOk {
  documents: DocRecord[];
}

export interface TraceStep {
  step: string;
  ts: string;
  hits?: number;
  scores?: unknown;
  queries?: unknown;
  ms?: number;
  filter?: unknown;
  refs?: number;
  [k: string]: unknown;
}

export interface TraceRecord {
  trace_id: string;
  query: string;
  created_at: string;
  steps: TraceStep[];
  degraded: string[];
}

export interface TraceResp extends ApiOk {
  trace?: TraceRecord;
  error?: string;
}

export interface QueryReq {
  query: string;
  k: number;
  filter?: Record<string, unknown>;
  rewrite?: boolean;
  self_query?: boolean;
  generate_answer?: boolean;
  use_cache?: boolean;
}
