// Real-time event stream (WebSocket) shared across the app.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, tokenStore } from "./api";

type LiveEvent = { type: string; [k: string]: unknown };
type Listener = (e: LiveEvent) => void;

export interface Toast { id: number; title: string; message: string; priority: string }

interface LiveState {
  connected: boolean;
  metrics: Record<string, Record<string, number>>;
  history: Record<string, { t: number; v: Record<string, number> }[]>;
  health: number | null;
  toasts: Toast[];
  dismiss: (id: number) => void;
  subscribe: (fn: Listener) => () => void;
}

const LiveContext = createContext<LiveState | null>(null);
const HISTORY = 120; // live samples kept per device (~10 min at 5 s)

export function LiveProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [metrics, setMetrics] = useState<LiveState["metrics"]>({});
  const [history, setHistory] = useState<LiveState["history"]>({});
  const [health, setHealth] = useState<number | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const listeners = useRef(new Set<Listener>());

  const subscribe = useCallback((fn: Listener) => {
    listeners.current.add(fn);
    return () => { listeners.current.delete(fn); };
  }, []);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let ping: number | undefined;
    let closed = false;

    const connect = () => {
      const token = tokenStore.get();
      if (!token) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
      ws.onopen = () => {
        setConnected(true);
        ping = window.setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
      };
      ws.onclose = () => {
        setConnected(false);
        window.clearInterval(ping);
        if (!closed) retry = window.setTimeout(connect, 3000);
      };
      ws.onmessage = (msg) => {
        const e = JSON.parse(msg.data) as LiveEvent;
        if (e.type === "metrics") {
          const devices = e.devices as Record<string, Record<string, number>>;
          const t = Date.parse(e.time as string);
          setMetrics((m) => ({ ...m, ...devices }));
          setHistory((h) => {
            const next = { ...h };
            for (const [id, v] of Object.entries(devices)) {
              next[id] = [...(h[id] ?? []), { t, v }].slice(-HISTORY);
            }
            return next;
          });
        } else if (e.type === "health") {
          setHealth(e.score as number);
        } else if (e.type === "notification") {
          const toast = { id: Date.now() + Math.random(), title: e.title as string, message: e.message as string,
            priority: e.priority as string };
          setToasts((t) => [...t.slice(-3), toast]);
          window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== toast.id)), 8000);
        }
        listeners.current.forEach((fn) => fn(e));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      window.clearInterval(ping);
      ws?.close();
    };
  }, []);

  return (
    <LiveContext.Provider value={{ connected, metrics, history, health, toasts, dismiss, subscribe }}>
      {children}
    </LiveContext.Provider>
  );
}

export function useLive() {
  const ctx = useContext(LiveContext);
  if (!ctx) throw new Error("useLive outside LiveProvider");
  return ctx;
}

/** Fetch `path`, and refetch (throttled) whenever a live event of one of `refreshOn` types arrives. */
export function useApi<T>(path: string | null, refreshOn: string[] = [], intervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { subscribe } = useLive();
  const pending = useRef<number | undefined>(undefined);
  const key = refreshOn.join(",");

  const load = useCallback(async () => {
    if (!path) return;
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => { setLoading(true); load(); }, [load]);

  useEffect(() => {
    if (!key) return;
    const types = new Set(key.split(","));
    return subscribe((e) => {
      if (!types.has(e.type) || pending.current) return;
      pending.current = window.setTimeout(() => { pending.current = undefined; load(); }, 1500);
    });
  }, [key, load, subscribe]);

  useEffect(() => {
    if (!intervalMs) return;
    const id = window.setInterval(load, intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, load]);

  return { data, error, loading, reload: load };
}
