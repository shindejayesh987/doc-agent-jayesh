import type {
  Collection,
  Document,
  DocumentData,
  Provider,
  Conversation,
  Message,
  UploadResult,
} from "./types";

// All calls go through /api/backend/[...path] server-side proxy,
// which reads the Supabase session from cookies and injects the
// Authorization header before forwarding to FastAPI.
const PREFIX = "/api/backend";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PREFIX}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

// ── Health ──────────────────────────────────────────────────────────────────

export async function getHealth(): Promise<{ status: string; supabase: string }> {
  return apiFetch("/health");
}

// ── Collections ─────────────────────────────────────────────────────────────

export async function getCollections(): Promise<Collection[]> {
  return apiFetch<Collection[]>("/collections");
}

export async function getCollectionDocuments(collectionId: string): Promise<Document[]> {
  return apiFetch<Document[]>(`/collections/${collectionId}/documents`);
}

// ── Documents ───────────────────────────────────────────────────────────────

export async function getDocuments(): Promise<Document[]> {
  return apiFetch<Document[]>("/documents");
}

export async function getDocument(docId: string): Promise<DocumentData> {
  return apiFetch<DocumentData>(`/documents/${docId}`);
}

export async function uploadDocuments(files: File[], collectionId?: string): Promise<UploadResult> {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  if (collectionId) {
    formData.append("collection_id", collectionId);
  }
  // Don't set Content-Type — browser sets it with multipart boundary automatically.
  // Auth is injected by the server-side proxy.
  const res = await fetch(`${PREFIX}/documents/upload`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

export async function deleteDocument(docId: string): Promise<void> {
  await apiFetch(`/documents/${docId}`, { method: "DELETE" });
}

export function subscribeToIndexingProgress(
  docId: string,
  onProgress: (data: { percentage: number; step: string; log: string }) => void,
  onDone: () => void,
  onError: (error: string) => void,
): () => void {
  const es = new EventSource(`${PREFIX}/documents/indexing-progress/${docId}`);

  es.addEventListener("progress", (e) => {
    onProgress(JSON.parse(e.data));
  });
  es.addEventListener("done", () => {
    onDone();
    es.close();
  });
  es.addEventListener("error", (e) => {
    if (e instanceof MessageEvent) {
      const data = JSON.parse(e.data);
      onError(data.error);
    }
    es.close();
  });

  return () => es.close();
}

// ── Providers ───────────────────────────────────────────────────────────────

export async function getProviders(): Promise<Record<string, Provider>> {
  return apiFetch<Record<string, Provider>>("/providers");
}

export async function connectProvider(
  provider: string,
  model: string,
  apiKey: string,
): Promise<{ status: string; provider: string; model: string; label: string }> {
  return apiFetch("/providers/connect", {
    method: "POST",
    body: JSON.stringify({ provider, model, api_key: apiKey }),
  });
}

// ── Chat ────────────────────────────────────────────────────────────────────

export async function chat(
  messages: Array<{ role: string; content: string }>,
  docIds: string[],
  conversationId?: string,
): Promise<{ role: string; content: string; latency_ms: number; lexical_grounding_score: number }> {
  return apiFetch("/chat", {
    method: "POST",
    body: JSON.stringify({
      messages,
      doc_ids: docIds,
      conversation_id: conversationId,
    }),
  });
}

// ── Conversations ───────────────────────────────────────────────────────────

export async function getConversations(): Promise<Conversation[]> {
  return apiFetch<Conversation[]>("/conversations");
}

export async function createConversation(
  title: string,
  docIds: string[],
): Promise<Conversation> {
  return apiFetch("/conversations", {
    method: "POST",
    body: JSON.stringify({ title, doc_ids: docIds }),
  });
}

export async function getConversationMessages(
  convId: string,
): Promise<Message[]> {
  return apiFetch<Message[]>(`/conversations/${convId}/messages`);
}

export async function deleteConversation(convId: string): Promise<void> {
  await apiFetch(`/conversations/${convId}`, { method: "DELETE" });
}
