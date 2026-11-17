"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { client, setToken, setUnauthorizedHandler, unwrap, type Schemas } from "./api";

type User = Schemas["UserOut"];
const KEY = "tripplanner.auth";

interface AuthCtx {
  user: User | null;
  ready: boolean;
  login: (email: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(KEY);
      if (raw) {
        const s = JSON.parse(raw) as { token: string; user: User };
        setToken(s.token);
        setUser(s.user);
      }
    } catch {
      /* ignore unreadable storage */
    }
    setReady(true);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    try {
      sessionStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
    router.replace("/login");
  }, [router]);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(async (email: string) => {
    const r = await unwrap(client.POST("/auth/dev-login", { body: { email } }));
    setToken(r.access_token);
    setUser(r.user);
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ token: r.access_token, user: r.user }));
    } catch {
      /* in-memory only */
    }
  }, []);

  const value = useMemo(() => ({ user, ready, login, logout }), [user, ready, login, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth outside AuthProvider");
  return c;
}

/** Redirects to /login when there is no session. Returns the user once known. */
export function useRequireAuth(): User | null {
  const { user, ready } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (ready && !user) router.replace("/login");
  }, [ready, user, router]);
  return user;
}
