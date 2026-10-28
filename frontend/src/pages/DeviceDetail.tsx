import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowDown, ArrowUp, Play, Wrench } from "lucide-react";
import { api, post, type DeviceDetail as Detail, type Playbook } from "../api";
import { useAuth } from "../auth";
import { useApi, useLive } from "../live";
import { TrendChart } from "../components/charts";
import { Badge, Empty, Meter, PageHeader, SeverityIcon, ago, fmtMetric } from "../components/ui";

const CHARTS = [
  { metric: "cpu", color: "#38bdf8", unit: "%" },
  { metric: "memory", color: "#a78bfa", unit: "%" },
  { metric: "traffic_in", color: "#34d399", unit: " Mbps" },
  { metric: "latency", color: "#fbbf24", unit: " ms" },
  { metric: "packet_loss", color: "#fb7185", unit: "%" },
  { metric: "if_errors", color: "#f97316", unit: "/min" },
];
const RANGES = [{ label: "Live", hours: 0 }, { label: "1h", hours: 1 }, { label: "24h", hours: 24 }, { label: "7d", hours: 168 }];

export default function DeviceDetail() {
  const { id } = useParams();
  const { can } = useAuth();
  const { data: d, reload } = useApi<Detail>(`/devices/${id}`, ["alert", "remediation"]);
  const { data: playbooks } = useApi<Playbook[]>("/playbooks");
  const { metrics, history } = useLive();
  const [range, setRange] = useState(1);
  const metricNames = CHARTS.map((c) => c.metric).join(",");
  const { data: hist } = useApi<{ series: Record<string, { t: number; v: number }[]> }>(
    range ? `/devices/${id}/metrics?metrics=${metricNames}&hours=${range}&points=240` : null);
  const [pb, setPb] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const live = useMemo(() => {
    const out: Record<string, { t: number; v: number }[]> = {};
    for (const { metric } of CHARTS) {
      out[metric] = (history[id ?? ""] ?? []).filter((s) => s.v[metric] !== undefined).map((s) => ({ t: s.t, v: s.v[metric] }));
    }
    return out;
  }, [history, id]);

  if (!d) return <Empty>Loading...</Empty>;
  const m = metrics[d.id] ?? d.metrics;
  const series = range ? hist?.series ?? {} : live;

  const toggleMaintenance = async () => {
    await api(`/devices/${d.id}`, { method: "PATCH", body: JSON.stringify({ maintenance_mode: !d.maintenance_mode }) });
    reload();
  };
  const runPlaybook = async () => {
    if (!pb) return;
    const res = await post<{ status: string }>("/remediation/runs", { playbook: pb, device_id: d.id });
    setMsg(`Run ${res.status ?? "submitted"} - follow it under Remediation`);
  };

  return (
    <>
      <PageHeader title={d.name} subtitle={`${d.type.replace("_", " ")} · ${d.ip ?? "no IP"} · ${d.site} · ${d.collector} collector`}
        actions={<div className="flex items-center gap-2"><Badge value={d.status} /><Badge value={d.criticality === "critical" ? "high" : d.criticality} />
          {can("operator") && <button className="btn-ghost" onClick={toggleMaintenance}>
            <Wrench className="h-4 w-4" /> {d.maintenance_mode ? "End maintenance" : "Maintenance mode"}</button>}</div>} />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card">
          <div className="card-title">Real-time metrics</div>
          <div className="space-y-3">
            {Object.entries(m).map(([k, v]) => {
              const cat = d.metric_catalog[k];
              return (
                <div key={k}>
                  <div className="flex justify-between text-sm"><span className="text-slate-400">{cat?.label ?? k}</span>
                    <span className="tabular-nums text-slate-200">{fmtMetric(k, v)}</span></div>
                  {(cat?.max || k.startsWith("traffic")) && <Meter value={v} max={k.startsWith("traffic") ? d.capacity_mbps : cat!.max!} />}
                </div>
              );
            })}
          </div>
          <div className="mt-4 text-xs text-slate-500">Last seen {ago(d.last_seen)} · capacity {d.capacity_mbps} Mbps</div>
        </div>
        <div className="space-y-4 lg:col-span-2">
          <div className="card">
            <div className="mb-3 flex items-center justify-between">
              <div className="card-title mb-0">Metrics</div>
              <div className="flex gap-1">{RANGES.map((r) => (
                <button key={r.label} onClick={() => setRange(r.hours)}
                  className={`rounded-md px-2 py-1 text-xs ${range === r.hours ? "bg-brand-600 text-white" : "text-slate-400 hover:bg-slate-800"}`}>{r.label}</button>))}
              </div>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              {CHARTS.filter((c) => c.metric in m).map((c) => (
                <div key={c.metric}>
                  <div className="mb-1 text-xs text-slate-400">{d.metric_catalog[c.metric]?.label}</div>
                  {(series[c.metric]?.length ?? 0) > 1 ? <TrendChart data={series[c.metric]} color={c.color} unit={c.unit} height={130} long={range > 24} />
                    : <div className="flex h-[130px] items-center justify-center text-xs text-slate-600">waiting for data...</div>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="card">
          <div className="card-title">Dependencies</div>
          {d.upstream.map((u) => <Link key={u.id} to={`/devices/${u.id}`} className="flex items-center gap-2 py-1 text-sm hover:text-brand-400">
            <ArrowUp className="h-3 w-3 text-slate-500" />{u.name} <span className="text-xs text-slate-600">{u.kind}</span></Link>)}
          {d.downstream.map((u) => <Link key={u.id} to={`/devices/${u.id}`} className="flex items-center gap-2 py-1 text-sm hover:text-brand-400">
            <ArrowDown className="h-3 w-3 text-slate-500" />{u.name}</Link>)}
          {!d.upstream.length && !d.downstream.length && <Empty>No links</Empty>}
        </div>
        <div className="card">
          <div className="card-title">Recent changes</div>
          {d.recent_changes.length === 0 ? <Empty>No changes recorded</Empty> : (
            <ul className="space-y-2 text-sm">{d.recent_changes.map((c, i) => (
              <li key={i}><span className="text-xs text-slate-500">{ago(c.time)}</span>
                <div className="text-slate-300">{c.summary}</div></li>))}</ul>)}
        </div>
        <div className="card">
          <div className="card-title">Alerts</div>
          {d.alerts.length === 0 ? <Empty>No alerts</Empty> : (
            <ul className="space-y-2 text-sm">{d.alerts.map((a) => (
              <li key={a.id} className="flex items-start gap-2"><SeverityIcon severity={a.status === "resolved" ? "ok" : a.severity} className="mt-0.5 h-4 w-4 shrink-0" />
                <div><div className="text-slate-300">{a.title}</div><div className="text-xs text-slate-500">{a.kind} · {ago(a.first_seen)} · {a.status}</div></div></li>))}</ul>)}
          {can("operator") && (
            <div className="mt-4 border-t border-slate-800 pt-3">
              <div className="mb-2 text-xs text-slate-400">Run a playbook manually</div>
              <div className="flex gap-2">
                <select className="input" value={pb} onChange={(e) => setPb(e.target.value)}>
                  <option value="">Choose playbook...</option>
                  {playbooks?.map((p) => <option key={p.name} value={p.name}>{p.title}</option>)}
                </select>
                <button className="btn-primary" onClick={runPlaybook} disabled={!pb}><Play className="h-4 w-4" /></button>
              </div>
              {msg && <div className="mt-2 text-xs text-emerald-300">{msg}</div>}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
