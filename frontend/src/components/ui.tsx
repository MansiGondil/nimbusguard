import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleHelp, Info, XCircle } from "lucide-react";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

const SEVERITY: Record<string, string> = {
  critical: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  warning: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  info: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
};
const STATUS: Record<string, string> = {
  up: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  degraded: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  down: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  unknown: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  open: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  acknowledged: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  mitigating: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  resolved: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  succeeded: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  running: "bg-sky-500/15 text-sky-300 ring-sky-500/30 animate-pulse",
  approved: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  pending_approval: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  rolled_back: "bg-orange-500/15 text-orange-300 ring-orange-500/30",
  escalated: "bg-orange-500/15 text-orange-300 ring-orange-500/30",
  failed: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  rejected: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  blocked: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  low: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  medium: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  high: "bg-orange-500/15 text-orange-300 ring-orange-500/30",
};

export function Badge({ value, kind = "status" }: { value: string; kind?: "status" | "severity" }) {
  const map = kind === "severity" ? SEVERITY : STATUS;
  return (
    <span className={cx("inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
      map[value] ?? STATUS.unknown)}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function SeverityIcon({ severity, className = "h-4 w-4" }: { severity: string; className?: string }) {
  if (severity === "critical") return <XCircle className={cx(className, "text-rose-400")} />;
  if (severity === "warning") return <AlertTriangle className={cx(className, "text-amber-400")} />;
  if (severity === "info") return <Info className={cx(className, "text-sky-400")} />;
  if (severity === "ok") return <CheckCircle2 className={cx(className, "text-emerald-400")} />;
  return <CircleHelp className={cx(className, "text-slate-400")} />;
}

export function StatCard({ label, value, sub, icon, tone = "default" }: {
  label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const tones = { default: "text-slate-100", good: "text-emerald-300", warn: "text-amber-300", bad: "text-rose-300" };
  return (
    <div className="card">
      <div className="flex items-center justify-between text-xs font-semibold uppercase tracking-wide text-slate-400">
        {label}
        {icon && <span className="text-slate-500">{icon}</span>}
      </div>
      <div className={cx("mt-2 text-3xl font-semibold tabular-nums", tones[tone])}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-sm text-slate-500">{children}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export function ago(iso: string | null | undefined) {
  if (!iso) return "-";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 0) return `in ${until(iso)}`;
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function until(iso: string | null | undefined) {
  if (!iso) return "-";
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (s <= 0) return "now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

export function fmtBytes(n: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`;
}

export function fmtMetric(metric: string, v: number | undefined) {
  if (v === undefined || v === null) return "-";
  if (metric.startsWith("traffic")) return v >= 1000 ? `${(v / 1000).toFixed(2)} Gbps` : `${v.toFixed(0)} Mbps`;
  if (["cpu", "memory", "disk", "packet_loss"].includes(metric)) return `${v.toFixed(metric === "packet_loss" ? 2 : 0)}%`;
  if (metric === "latency") return `${v.toFixed(1)} ms`;
  if (metric === "temperature") return `${v.toFixed(0)}°C`;
  if (metric === "if_errors") return `${v.toFixed(1)}/min`;
  return v.toFixed(2);
}

export function Meter({ value, max = 100 }: { value: number | undefined; max?: number }) {
  const pct = Math.min(100, Math.max(0, ((value ?? 0) / max) * 100));
  const color = pct > 90 ? "bg-rose-500" : pct > 75 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
      <div className={cx("h-full rounded-full transition-all duration-700", color)} style={{ width: `${pct}%` }} />
    </div>
  );
}
