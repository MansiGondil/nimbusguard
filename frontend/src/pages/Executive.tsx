import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Clock, DollarSign, ShieldCheck, TrendingUp, Wrench } from "lucide-react";
import type { Executive as ExecutiveData } from "../api";
import { useApi, useLive } from "../live";
import { HealthGauge, TrendChart } from "../components/charts";
import { Empty, PageHeader, StatCard } from "../components/ui";

export default function Executive() {
  const { data } = useApi<ExecutiveData>("/dashboard/executive", ["remediation", "incident"], 60000);
  const { health } = useLive();
  const score = health ?? data?.health_score ?? null;
  const verdict = score === null ? "Collecting data..." : score >= 95 ? "Network is healthy" :
    score >= 85 ? "Minor issues being handled" : "Attention needed";

  return (
    <>
      <PageHeader title="Executive overview" subtitle="The big picture for leadership, last 30 days" />
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card flex flex-col items-center justify-center gap-2 py-6">
          <HealthGauge score={score} size={180} />
          <div className="text-sm font-medium text-slate-300">{verdict}</div>
        </div>
        <div className="grid grid-cols-2 gap-4 lg:col-span-2">
          <StatCard label="Uptime this month" icon={<TrendingUp className="h-4 w-4" />}
            value={data ? `${data.uptime_percent.toFixed(2)}%` : "-"}
            tone={data && data.uptime_percent >= 99.9 ? "good" : "warn"} sub="Target 99.9%" />
          <StatCard label="Incidents prevented" icon={<ShieldCheck className="h-4 w-4" />}
            value={data?.incidents_prevented ?? "-"} tone="good" sub={`${data?.automated_fixes ?? 0} automated fixes`} />
          <StatCard label="Estimated savings" icon={<DollarSign className="h-4 w-4" />}
            value={data ? `$${data.money_saved.toLocaleString()}` : "-"} tone="good"
            sub={data ? `${data.downtime_hours_avoided} h downtime avoided × $${data.cost_model.downtime_cost_per_hour}/h` : ""} />
          <StatCard label="Auto-fix success" icon={<Wrench className="h-4 w-4" />}
            value={data?.remediation_success_rate != null ? `${data.remediation_success_rate}%` : "-"}
            sub={<span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" /> MTTR {data?.mttr_minutes != null ? `${data.mttr_minutes} min` : "-"}</span>} />
        </div>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="card">
          <div className="card-title">Network health, last 7 days</div>
          {data?.health_trend.length ? <TrendChart data={data.health_trend} color="#34d399" unit="%" domain={[80, 100]} long height={220} />
            : <Empty>No history yet</Empty>}
        </div>
        <div className="card">
          <div className="card-title">Incidents vs. prevented</div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data?.incidents_by_day ?? []} margin={{ left: -20 }}>
              <CartesianGrid stroke="#1e293b" vertical={false} />
              <XAxis dataKey="day" stroke="#475569" fontSize={11} tickFormatter={(d) => d.slice(5)} />
              <YAxis stroke="#475569" fontSize={11} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: 8 }} cursor={{ fill: "#1e293b" }} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="incidents" fill="#64748b" radius={[4, 4, 0, 0]} />
              <Bar dataKey="prevented" fill="#34d399" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <p className="mt-4 text-xs text-slate-500">
        Savings use a configurable cost model ({data?.cost_model.avg_incident_hours_avoided ?? "-"} h of downtime avoided per
        prevented incident at ${data?.cost_model.downtime_cost_per_hour ?? "-"}/h). Set NG_DOWNTIME_COST_PER_HOUR to match your organisation.
      </p>
    </>
  );
}
