import { type UIMessage } from "ai";
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function POST(req: Request) {
  const body = await req.json();
  const {
    messages,
    docIds,
    conversationId,
  }: {
    messages: UIMessage[];
    docIds?: string[];
    conversationId?: string;
  } = body;

  // Convert UIMessages to simple role/content pairs for our backend
  const simpleMessages = messages.map((m) => ({
    role: m.role,
    content:
      m.parts
        ?.filter((p): p is { type: "text"; text: string } => p.type === "text")
        .map((p) => p.text)
        .join("\n") || "",
  }));

  // Get Supabase session token to forward to backend
  const cookieStore = await cookies();
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() { return cookieStore.getAll(); },
        setAll() {},
      },
    },
  );
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;

  const backendRes = await fetch(`${API_URL}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      messages: simpleMessages,
      doc_ids: docIds || [],
      conversation_id: conversationId || null,
    }),
  });

  if (!backendRes.ok) {
    // Parse backend error but only forward user-safe messages
    const error = await backendRes.json().catch(() => ({
      detail: "Something went wrong",
    }));
    const safeDetail = error.detail || "Something went wrong";
    return new Response(JSON.stringify({ error: safeDetail }), {
      status: backendRes.status,
      headers: { "Content-Type": "application/json" },
    });
  }

  const result = await backendRes.json();
  const content = result.content || "";

  // Return as AI SDK v6 UIMessageStream format (SSE with JSON chunks)
  // Each chunk needs an `id` to identify the text part
  const partId = crypto.randomUUID();
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "text-start", id: partId })}\n\n`));
      controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "text-delta", id: partId, delta: content })}\n\n`));
      controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "text-end", id: partId })}\n\n`));
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "x-vercel-ai-ui-message-stream": "v1",
      "x-accel-buffering": "no",
    },
  });
}
