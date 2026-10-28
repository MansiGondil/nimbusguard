// Typed API client for the NimbusGuard backend.

export type Severity = "info" | "warning" | "critical";
export type DeviceStatus = "up" | "degraded" | "down" | "unknown";

export interface User { id: string; username: string; role: "viewer" | "operator" | "admin" }

export interface Device {
  id: string; name: string; type: string; ip: string | null; site: string; vendor: string | null;
  criticality: string; status: DeviceStatus; collector: string; capacity_mbps: number; services: string[];
  maintenance_mode: boolean; last_seen: string | null; metrics: Record<string, number>;
}

export interface DeviceDetail extends Device {
  upstream: { id: string; name: string; kind: string }[];
  downstream: { id: string; name: string; kind: string }[];
  recent_changes: { time: string; kind: string; summary: string; run_id?: string }[];
  alerts: { id: string; title: string; severity: Severity; status: string; kind: string; first_seen: string }[];
  metric_catalog: Record<string, { unit: string; max: number | null; label: string }>;
}

export interface Alert {
  id: string; device_id: string | null; device: string | null; kind: string; metric: string | null;
  severity: Severity; title: string; detail: Record<string, unknown>; score: number; status: string;
  incident_id: string | null; acknowledged_by: string | null; first_seen: string; last_seen: string;
  resolved_at: string | null;
}

export interface ChainStep {
  device_id: string; device: string; status: "normal" | "abnormal"; finding: string;
  latency?: number | null; packet_loss?: number | null;
}

export interface Incident {
  id: string; title: string; severity: Severity; status: string; root_cause_device_id: string | null;
  root_cause_device: string | null; prevented: boolean; created_at: string; resolved_at: string | null;
  alert_count: number | null;
  rca: {
    root_cause?: string; diagnosis?: string; recommendation?: string; signature?: string; confidence?: number;
    investigation_chain?: ChainStep[]; affected_devices?: string[];
    impact?: { downstream_devices: number; alerting_devices: number; grouped_alerts: number };
  };
  alerts?: Alert[];
  remediations?: { id: string; playbook: string; status: string; created_at: string }[];
}

export interface Gate { name: string; passed: boolean; detail: string; effect: string | null }
export interface RunStep {
  phase: string; action: string; params: Record<string, unknown>; status: string;
  result: Record<string, unknown>; started_at: string; finished_at: string;
}
export interface Run {
  id: string; playbook: string; title: string; risk: string | null; device_id: string; device: string | null;
  alert_id: string | null; status: string; reason: string | null; gates: Gate[]; steps: RunStep[];
  driver: string | null; requested_by: string; approved_by: string | null; created_at: string;
  finished_at: string | null;
}

export interface Playbook {
  name: string; title: string; description: string; triggers: string[]; kinds: string[]; risk: string;
  requires_approval: boolean; steps: { action: string; params: Record<string, unknown>; when?: string }[];
  rollback: { action: string }[];
}

export interface Prediction {
  id: string; device_id: string; device: string | null; summary: string; probability: number;
  eta: string | null; kind: string; metric?: string | null;
}

export interface Executive {
  health_score: number | null; uptime_percent: number; incidents_prevented: number; automated_fixes: number;
  remediation_success_rate: number | null; mttr_minutes: number | null; money_saved: number;
  downtime_hours_avoided: number; cost_model: { downtime_cost_per_hour: number; avg_incident_hours_avoided: number };
  health_trend: { t: number; v: number }[];
  incidents_by_day: { day: string; incidents: number; prevented: number }[];
}

export interface Operations {
  health_score: number | null; devices_total: number; devices_online: number;
  status_counts: Record<string, number>; open_incidents: number; pending_approvals: number;
  active_remediations: number;
  current_issues: (Pick<Alert, "id" | "device_id" | "device" | "title" | "severity" | "kind" | "status" |
    "first_seen" | "incident_id">)[];
  predicted_issues: Prediction[];
  recent_logs: { time: string; host: string; severity: number; category: string; message: string }[];
  top_talkers: { src: string; dst: string; bytes: number }[];
  simulated_faults: Fault[];
}

export interface Fault { kind: string; device_id: string; device: string; started: string; stubborn: boolean }

export interface Topology {
  nodes: { id: string; name: string; type: string; status: DeviceStatus; criticality: string; site: string }[];
  edges: { source: string; target: string; kind: string }[];
}

export interface AuditEntry {
  id: number; time: string; actor: string; action: string; target: string | null;
  detail: Record<string, unknown>; hash: string;
}

const TOKEN_KEY = "ng_token";

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = tokenStore.get();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof URLSearchParams)) headers.set("Content-Type", "application/json");
  const res = await fetch(`/api${path}`, { ...init, headers });
  if (res.status === 401) {
    tokenStore.clear();
    window.dispatchEvent(new Event("ng:logout"));
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export async function login(username: string, password: string) {
  const res = await api<{ access_token: string; user: User }>("/auth/login", {
    method: "POST",
    body: new URLSearchParams({ username, password }),
  });
  tokenStore.set(res.access_token);
  return res.user;
}
