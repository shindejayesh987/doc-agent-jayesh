import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

/**
 * Server-side proxy for all /api/backend/* requests.
 *
 * Reads the Supabase session from cookies (server-side),
 * injects the Authorization header, and forwards to FastAPI.
 * Retries on ECONNREFUSED to handle cold-start race conditions.
 */
async function proxyToBackend(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const backendPath = `/api/${path.join("/")}`;

  // Read Supabase session server-side from cookies
  const cookieStore = await cookies();
  const allCookies = cookieStore.getAll();

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return allCookies;
        },
        setAll() {},
      },
    },
  );
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;

  // Build headers
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  // Forward query string
  const url = new URL(request.url);
  const targetUrl = `${BACKEND_URL}${backendPath}${url.search}`;

  // Forward body for non-GET/HEAD requests
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  // Retry loop for cold-start race conditions (backend may not be ready yet)
  let lastError: Error | null = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const backendRes = await fetch(targetUrl, {
        method: request.method,
        headers,
        body: hasBody ? request.body : undefined,
        // @ts-expect-error — duplex required for streaming request body in Node
        duplex: hasBody ? "half" : undefined,
      });

      // Stream the response back (important for SSE endpoints)
      const responseHeaders = new Headers();
      backendRes.headers.forEach((value, key) => {
        if (!["transfer-encoding", "connection"].includes(key.toLowerCase())) {
          responseHeaders.set(key, value);
        }
      });

      return new Response(backendRes.body, {
        status: backendRes.status,
        statusText: backendRes.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      lastError = err as Error;
      const isConnectionRefused =
        lastError.cause &&
        typeof lastError.cause === "object" &&
        "code" in lastError.cause &&
        lastError.cause.code === "ECONNREFUSED";

      if (isConnectionRefused && attempt < MAX_RETRIES) {
        console.log(
          `[proxy] ${backendPath} | backend not ready, retry ${attempt + 1}/${MAX_RETRIES}`,
        );
        await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
        continue;
      }
      break;
    }
  }

  console.error(`[proxy] ${backendPath} | failed after retries:`, lastError?.message);
  return new Response(
    JSON.stringify({ detail: "Backend service is starting up. Please try again in a moment." }),
    {
      status: 503,
      headers: { "Content-Type": "application/json" },
    },
  );
}

export const GET = proxyToBackend;
export const POST = proxyToBackend;
export const PUT = proxyToBackend;
export const DELETE = proxyToBackend;
export const PATCH = proxyToBackend;
