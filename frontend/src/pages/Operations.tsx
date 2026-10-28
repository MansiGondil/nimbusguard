import { Link } from "react-router-dom";
import { Activity, AlertTriangle, Sparkles, Server, Wrench } from "lucide-react";
import type { Operations as OpsData } from "../api";
import { useApi, useLive } from "../live";
import { Badge, Empty, PageHeader, SeverityIcon, StatCard, ago, fmtBytes, until } from "../components/ui";

const SYSLOG_SEV = ["emerg", "alert", "crit", "error", "warn", "notice", "info", "debug"];

export default function Operations() {
  const { data } = useApi<OpsData>("/dashboard/operations", ["alert", "incident", "remediation", "prediction", "health"], 15000);
  const { health } = useLive();
  if (!data) return <Empty>Loading...</Empty>;

  return (
    <>
      <PageHeader title="Operations" subtitle="Live view for the IT team" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Active devices" icon={<Server className="h-4 w-4" />}
          value={`${data.devices_online}/${data.devices_total}`}
          tone={data.devices_online === data.devices_total ? "good" : "bad"}
          sub={`${data.status_counts.degraded ?? 0} degraded · ${data.status_counts.down ?? 0} down`} />
        <StatCard label="Health" icon={<Activity className="h-4 w-4" />} value={`${health ?? data.health_score ?? "-"}%`} />
        <StatCard label="Open incidents" icon={<AlertTriangle className="h-4 w-4" />} value={data.open_incidents}
          tone={data.open_incidents ? "warn" : "good"} />
        <StatCard label="Remediation" icon={<Wrench className="h-4 w-4" />} value={data.active_remediations}
          sub={<Link to="/remediation" className={data.pending_approvals ? "font-semibold text-amber-300" : ""}>
            {data.pending_approvals} awaiting approval</Link>} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="card">
          <div className="card-title"><AlertTriangle className="h-4 w-4" /> Current issues</div>
          {data.current_issues.length === 0 ? <Empty>All systems normal</Empty> : (
            <ul className="max-h-[28rem] divide-y divide-slate-800 overflow-y-auto pr-1">
              {data.current_issues.map((a) => (
                <li key={a.id} className="flex items-start gap-3 py-2">
                  <SeverityIcon severity={a.severity} className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-slate-200">
                      <Link to={`/devices/${a.device_id}`} className="font-medium hover:text-brand-400">{a.device}</Link>: {a.title}
                    </div>
                    <div className="text-xs text-slate-500">{a.kind} · {ago(a.first_seen)}
                      {a.incident_id && <> · <Link to={`/incidents/${a.incident_id}`} className="text-brand-400">incident</Link></>}</div>
                  </div>
                  <Badge value={a.status} />
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="card">
          <div className="card-title"><Sparkles className="h-4 w-4" /> Predicted issues (next 24h)</div>
          {data.predicted_issues.length === 0 ? <Empty>No failures forecast</Empty> : (
            <ul className="divide-y divide-slate-800">
              {data.predicted_issues.map((p) => (
                <li key={p.id} className="flex items-center gap-3 py-2">
                  <Sparkles className="h-4 w-4 shrink-0 text-violet-400" />
                  <div className="flex-1 text-sm">
                    <Link to={`/devices/${p.device_id}`} className="font-medium hover:text-brand-400">{p.device}</Link>: {p.summary}
                    <div className="text-xs text-slate-500">ETA {until(p.eta)} · confidence {Math.round(p.probability * 100)}%</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2">
          <div className="card-title">Syslog stream</div>
          <div className="max-h-72 overflow-y-auto font-mono text-xs">
            {data.recent_logs.map((l, i) => (
              <div key={i} className="flex gap-2 border-b border-slate-800/50 py-1">
                <span className="text-slate-500">{new Date(l.time).toLocaleTimeString()}</span>
                <span className={l.severity <= 3 ? "text-rose-400" : l.severity === 4 ? "text-amber-400" : "text-slate-400"}>
                  {SYSLOG_SEV[l.severity] ?? l.severity}</span>
                <span className="text-sky-300">{l.host}</span>
                <span className="truncate text-slate-300">{l.message}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-title">Top talkers (NetFlow, 15 min)</div>
          <table className="table">
            <tbody>
              {data.top_talkers.map((t, i) => (
                <tr key={i}>
                  <td className="font-mono text-xs">{t.src} → {t.dst}</td>
                  <td className="text-right tabular-nums">{fmtBytes(t.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
