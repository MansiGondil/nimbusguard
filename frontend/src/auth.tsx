import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, login as apiLogin, tokenStore, type User } from "./api";

interface AuthState {
  user: User | null;
  ready: boolean;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void;
  can: (role: User["role"]) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);
const RANK = { viewer: 0, operator: 1, admin: 2 };

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!tokenStore.get()) { setReady(true); return; }
    api<User>("/auth/me").then(setUser).catch(() => tokenStore.clear()).finally(() => setReady(true));
    const onLogout = () => setUser(null);
    window.addEventListener("ng:logout", onLogout);
    return () => window.removeEventListener("ng:logout", onLogout);
  }, []);

  const value: AuthState = {
    user,
    ready,
    login: async (u, p) => setUser(await apiLogin(u, p)),
    logout: () => { tokenStore.clear(); setUser(null); },
    can: (role) => !!user && RANK[user.role] >= RANK[role],
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
