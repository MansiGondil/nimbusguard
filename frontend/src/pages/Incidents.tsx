import { Link, useParams } from "react-router-dom";
import { CheckCheck, CheckCircle2, Lightbulb, Search, ShieldCheck, XCircle } from "lucide-react";
import { post, type Incident } from "../api";
import { useAuth } from "../auth";
import { useApi } from "../live";
import { Badge, Empty, PageHeader, SeverityIcon, ago, cx } from "../components/ui";

export default function Incidents() {
  const { id } = useParams();
  const { data } = useApi<Incident[]>("/incidents?limit=100", ["incident", "alert"]);
  const selected = id ?? data?.[0]?.id;

  return (
    <>
      <PageHeader title="Incidents" subtitle="Related alerts grouped automatically, with root-cause analysis" />
      <div className="grid gap-4 lg:grid-cols-5">
        <div className="card p-0 lg:col-span-2">
          {!data ? <Empty>Loading...</Empty> : data.length === 0 ? <Empty>No incidents yet. Try the Simulator.</Empty> : (
            <ul className="max-h-[75vh] divide-y divide-slate-800 overflow-y-auto">
              {data.map((i) => (
                <li key={i.id}>
                  <Link to={`/incidents/${i.id}`} className={cx("flex gap-3 p-3 hover:bg-slate-800/50", selected === i.id && "bg-slate-800/70")}>
                    <SeverityIcon severity={i.status === "resolved" ? "ok" : i.severity} className="mt-0.5 h-4 w-4 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-slate-200">{i.title}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                        <Badge value={i.status} /> {i.prevented && <Badge value="succeeded" />}
                        <span>{i.alert_count} alerts · {ago(i.created_at)}</span>
                      </div>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="lg:col-span-3">{selected ? <IncidentDetail id={selected} /> : null}</div>
      </div>
    </>
  );
}

function IncidentDetail({ id }: { id: string }) {
  const { data: inc, reload } = useApi<Incident>(`/incidents/${id}`, ["incident", "alert", "remediation"]);
  const { can } = useAuth();
  if (!inc) return <div className="card"><Empty>Loading...</Empty></div>;
  const rca = inc.rca;
  const ack = async () => { await post(`/incidents/${inc.id}/ack`); reload(); };

  return (
    <div className="space-y-4">
      <div className="card">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex items-center gap-2"><Badge value={inc.severity} kind="severity" /><Badge value={inc.status} />
              {inc.prevented && <span className="inline-flex items-center gap-1 text-xs text-emerald-300"><ShieldCheck className="h-3.5 w-3.5" /> auto-remediated</span>}</div>
            <h2 className="mt-2 text-lg font-semibold text-slate-100">{inc.title}</h2>
            <div className="text-xs text-slate-500">Opened {ago(inc.created_at)}{inc.resolved_at && ` · resolved ${ago(inc.resolved_at)}`}</div>
          </div>
          {can("operator") && inc.status !== "resolved" && (
            <button className="btn-ghost" onClick={ack}><CheckCheck className="h-4 w-4" /> Acknowledge</button>)}
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <Fact label="Root cause" value={rca.root_cause ?? "-"} />
          <Fact label="Impact" value={rca.impact ? `${rca.impact.downstream_devices} downstream devices · ${rca.impact.grouped_alerts} alerts` : "-"} />
          <Fact label="Confidence" value={rca.confidence ? `${Math.round(rca.confidence * 100)}%` : "-"} />
        </div>
        {rca.recommendation && (
          <div className="mt-3 flex items-start gap-2 rounded-lg bg-brand-600/10 p-3 text-sm text-brand-400">
            <Lightbulb className="mt-0.5 h-4 w-4 shrink-0" /> <span><b>Recommended fix:</b> {rca.recommendation}</span>
          </div>
        )}
      </div>

      {rca.investigation_chain && rca.investigation_chain.length > 0 && (
        <div className="card">
          <div className="card-title"><Search className="h-4 w-4" /> Investigation chain</div>
          <ol className="relative ml-2 border-l border-slate-700">
            {rca.investigation_chain.map((s, i) => (
              <li key={i} className="mb-4 ml-5">
                <span className="absolute -left-2.5 flex h-5 w-5 items-center justify-center rounded-full bg-slate-900">
                  {s.status === "normal" ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-rose-400" />}
                </span>
                <Link to={`/devices/${s.device_id}`} className="text-sm font-medium text-slate-200 hover:text-brand-400">{i + 1}. {s.device}</Link>
                <div className="text-xs text-slate-400">{s.finding}</div>
              </li>
            ))}
          </ol>
          <div className="text-xs text-slate-500">Walks the dependency path from the furthest affected device back to the root cause.</div>
        </div>
      )}

      <div className="card">
        <div className="card-title">Grouped alerts ({inc.alerts?.length ?? 0})</div>
        <ul className="divide-y divide-slate-800 text-sm">
          {inc.alerts?.map((a) => (
            <li key={a.id} className="flex items-center gap-2 py-1.5">
              <SeverityIcon severity={a.status === "resolved" ? "ok" : a.severity} />
              <span className="font-medium text-slate-300">{a.device}</span>
              <span className="flex-1 truncate text-slate-400">{a.title}</span>
              <span className="text-xs text-slate-600">{a.kind}</span>
            </li>
          ))}
        </ul>
        {inc.remediations && inc.remediations.length > 0 && (
          <div className="mt-3 border-t border-slate-800 pt-3 text-sm">
            {inc.remediations.map((r) => (
              <Link key={r.id} to="/remediation" className="flex items-center gap-2 hover:text-brand-400">
                Playbook <b>{r.playbook}</b> <Badge value={r.status} />
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-800/40 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-sm text-slate-200">{value}</div>
    </div>
  );
}
