export interface Collection {
  id: string;
  name: string;
  description: string;
  icon: string;
  is_global: boolean;
  doc_count: number;
  created_at: string;
}

export interface Document {
  id: string;
  name: string;
  page_count: number | null;
  total_tokens: number | null;
  status: "uploaded" | "indexing" | "indexed" | "failed";
  provider_used: string | null;
  model_used: string | null;
  indexing_duration_ms: number | null;
  created_at: string;
  indexed_at: string | null;
  error_message: string | null;
  collection_id: string | null;
  is_global: boolean;
}

export interface DocumentData {
  tree: Record<string, unknown>;
  pages: [string, number][];
  name: string;
}

export interface Provider {
  label: string;
  models: string[];
  free: boolean;
  key_hint: string;
}

export interface Conversation {
  id: string;
  title: string;
  doc_ids: string[];
  message_count: number;
  created_at: string;
  last_message_at: string | null;
}

export interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  sources?: Array<{ doc_id: string; node_id: string; title: string }>;
  model_used?: string;
  latency_ms?: number;
  lexical_grounding_score?: number;
  created_at?: string;
}

export interface IndexingProgress {
  percentage: number;
  step: string;
  log: string;
}

export interface UploadResult {
  documents: Array<{ doc_id: string; name: string }>;
}
