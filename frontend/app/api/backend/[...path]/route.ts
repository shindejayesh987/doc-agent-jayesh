import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

/**
 * Server-side proxy for all /api/backend/* requests.
 *
 * Reads the Supabase session from httpOnly cookies (server-side),
 * injects the Authorization header, and forwards to FastAPI.
 * This avoids the browser needing to read auth cookies via document.cookie.
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

  // Debug: log available cookies and env vars (remove after debugging)
  const supabaseCookies = allCookies
    .filter((c) => c.name.startsWith("sb-"))
    .map((c) => `${c.name}=${c.value.substring(0, 20)}...`);
  console.log(
    `[proxy] ${backendPath} | cookies: ${allCookies.length} total, sb-*: [${supabaseCookies.join(", ")}] | SUPABASE_URL: ${process.env.NEXT_PUBLIC_SUPABASE_URL ? "set" : "MISSING"}`,
  );

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

  console.log(
    `[proxy] ${backendPath} | session: ${session ? "found" : "NULL"} | token: ${token ? "yes" : "no"}`,
  );

  // Build headers — forward originals, inject auth, remove hop-by-hop headers
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

  const backendRes = await fetch(targetUrl, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    // @ts-expect-error — duplex required for streaming request body in Node
    duplex: hasBody ? "half" : undefined,
  });

  // Stream the response back (important for SSE endpoints like indexing-progress)
  const responseHeaders = new Headers();
  backendRes.headers.forEach((value, key) => {
    // Skip hop-by-hop headers
    if (!["transfer-encoding", "connection"].includes(key.toLowerCase())) {
      responseHeaders.set(key, value);
    }
  });

  return new Response(backendRes.body, {
    status: backendRes.status,
    statusText: backendRes.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxyToBackend;
export const POST = proxyToBackend;
export const PUT = proxyToBackend;
export const DELETE = proxyToBackend;
export const PATCH = proxyToBackend;
