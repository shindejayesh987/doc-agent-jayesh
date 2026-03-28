import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const searchParams = url.searchParams;
  const code = searchParams.get("code");
  const next = searchParams.get("next") ?? "/";

  // On Cloud Run, request.url uses the internal 0.0.0.0:8080 origin.
  // Use x-forwarded-host / x-forwarded-proto to build the real public origin.
  const forwardedHost = request.headers.get("x-forwarded-host");
  const forwardedProto = request.headers.get("x-forwarded-proto") ?? "https";
  const origin = forwardedHost
    ? `${forwardedProto}://${forwardedHost}`
    : url.origin;

  if (code) {
    const cookieStore = await cookies();

    // Collect cookies so we can explicitly apply them to the redirect response.
    // cookieStore.set() alone may not carry over to NextResponse.redirect().
    let responseCookies: Array<{
      name: string;
      value: string;
      options: Record<string, unknown>;
    }> = [];

    const supabase = createServerClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
      {
        cookies: {
          getAll() {
            return cookieStore.getAll();
          },
          setAll(cookiesToSet) {
            responseCookies = cookiesToSet;
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, {
                ...options,
                httpOnly: false,
              }),
            );
          },
        },
      },
    );

    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      // Debug: log cookies being set (remove after debugging)
      console.log(
        `[auth-callback] Setting ${responseCookies.length} cookies:`,
        responseCookies.map((c) => `${c.name} (${c.value.length} chars)`),
      );

      const response = NextResponse.redirect(`${origin}${next}`);
      // Explicitly set auth cookies on the redirect response so the
      // browser receives them regardless of how Next.js handles cookieStore.
      for (const { name, value, options } of responseCookies) {
        response.cookies.set(name, value, {
          ...options,
          httpOnly: false, // Browser Supabase client reads via document.cookie
        });
      }
      return response;
    }
  }

  // Auth error — redirect to login with error
  return NextResponse.redirect(`${origin}/login?error=auth`);
}
