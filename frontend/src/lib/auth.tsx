import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken } from "./api";

export type Me = {
  id: string;
  email: string;
  role: "user" | "manager" | "admin";
  team_id: string | null;
  onboarded_at: string | null;
};

type AuthCtx = {
  me: Me | null;
  ready: boolean;
  login(email: string, password: string): Promise<void>;
  logout(): void;
  refresh(): Promise<void>;
};

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const t = getToken();
    if (!t) { setReady(true); return; }
    api<Me>("/auth/me")
      .then((u) => setMe(u))
      .catch(() => setToken(null))
      .finally(() => setReady(true));
  }, []);

  async function login(email: string, password: string) {
    const r = await api<{ access_token: string; token_type: string }>(
      "/auth/token",
      { method: "POST", formBody: { username: email, password } }
    );
    setToken(r.access_token);
    const u = await api<Me>("/auth/me");
    setMe(u);
  }

  function logout() {
    setToken(null);
    setMe(null);
  }

  async function refresh() {
    try {
      const u = await api<Me>("/auth/me");
      setMe(u);
    } catch {
      // ignore; token-revoked case will be caught by api()'s 401 handling
    }
  }

  return <Ctx.Provider value={{ me, ready, login, logout, refresh }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>");
  return v;
}
