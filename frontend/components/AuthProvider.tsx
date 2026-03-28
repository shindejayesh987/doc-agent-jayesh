"use client";

import { createClient } from "@/lib/supabase-browser";
import { Session, User } from "@supabase/supabase-js";
import { useRouter } from "next/navigation";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
} from "react";

interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  avatarUrl: string | null;
  role: "admin" | "user";
  accessToken: string;
}

interface AuthContextType {
  user: AuthUser | null;
  loading: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  loading: true,
  signOut: async () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

const ADMIN_EMAILS = [
  "jay98shinde@gmail.com",
  "kadirlofca@outlook.com",
];

function sessionToAuthUser(session: Session): AuthUser {
  const user = session.user;
  const meta = user.user_metadata;
  return {
    id: user.id,
    email: user.email || "",
    displayName: meta?.full_name || meta?.name || user.email?.split("@")[0] || "User",
    avatarUrl: meta?.avatar_url || meta?.picture || null,
    role: ADMIN_EMAILS.includes(user.email || "") ? "admin" : "user",
    accessToken: session.access_token,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();
  const supabase = createClient();

  useEffect(() => {
    // Get initial session
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session) {
        setUser(sessionToAuthUser(session));
      }
      setLoading(false);
    });

    // Listen for auth changes
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session) {
        setUser(sessionToAuthUser(session));
      } else {
        setUser(null);
        router.replace("/login");
      }
      setLoading(false);
    });

    return () => subscription.unsubscribe();
  }, [router, supabase.auth]);

  async function signOut() {
    await supabase.auth.signOut();
    setUser(null);
    router.replace("/login");
  }

  return (
    <AuthContext.Provider value={{ user, loading, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}
