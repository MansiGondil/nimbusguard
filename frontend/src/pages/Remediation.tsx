import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, ChevronDown, ChevronRight, ShieldAlert, ShieldCheck, X } from "lucide-react";
import { post, type Playbook, type Run } from "../api";
import { useAuth } from "../auth";
import { useApi } from "../live";
import { Badge, Empty, PageHeader, ago, cx } from "../components/ui";

export default function Remediation() {
  const { data: runs, reload } = useApi<Run[]>("/remediation/runs?limit=100", ["remediation"]);
  const { data: playbooks } = useApi<Playbook[]>("/playbooks");
  const pending = runs?.filter((r) => r.status === "pending_approval") ?? [];
  const history = runs?.filter((r) => r.status !== "pending_approval") ?? [];

  return (
    <>
      <PageHeader title="Remediation" subtitle="Self-healing workflows with safety gates, verification and rollback" />
      <section className="mb-6">
        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-amber-300">
          <ShieldAlert className="h-4 w-4" /> Awaiting approval ({pending.length})</h2>
        {pending.length === 0 ? <div className="card"><Empty>Nothing needs a human right now</Empty></div> : (
          <div className="grid gap-3 lg:grid-cols-2">{pending.map((r) => <PendingCard key={r.id} run={r} onDone={reload} />)}</div>
        )}
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Run history</h2>
        <div className="card p-0">
          {!runs ? <Empty>Loading...</Empty> : history.length === 0 ? <Empty>No runs yet</Empty> :
            <ul className="divide-y divide-slate-800">{history.map((r) => <RunRow key={r.id} run={r} />)}</ul>}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Playbook catalogue</h2>
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
          {playbooks?.map((p) => (
            <div key={p.name} className="card">
              <div className="flex items-center justify-between"><span className="font-medium text-slate-200">{p.title}</span><Badge value={p.risk} /></div>
              <p className="mt-1 text-xs text-slate-400">{p.description}</p>
              <div className="mt-2 text-xs text-slate-500">Triggers: {p.triggers.join(", ")}</div>
              <ol className="mt-2 list-inside list-decimal text-xs text-slate-400">
                {p.steps.map((s, i) => <li key={i}>{s.action}{s.when ? ` (if ${s.when})` : ""}</li>)}
              </ol>
              {p.requires_approval && <div className="mt-2 text-xs text-amber-300">Always requires approval</div>}
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

function Gates({ run }: { run: Run }) {
  return (
    <ul className="mt-2 space-y-1 text-xs">
      {run.gates.map((g) => (
        <li key={g.name} className="flex items-start gap-1.5">
          {g.effect ? <ShieldAlert className={cx("mt-0.5 h-3.5 w-3.5 shrink-0", g.effect === "block" ? "text-rose-400" : "text-amber-400")} />
            : <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" />}
          <span className={g.effect ? "text-slate-200" : "text-slate-500"}><b className="font-medium">{g.name.replace(/_/g, " ")}</b>: {g.detail}</span>
        </li>
      ))}
    </ul>
  );
}

function PendingCard({ run, onDone }: { run: Run; onDone: () => void }) {
  const { can } = useAuth();
  const [busy, setBusy] = useState(false);
  const act = async (verb: "approve" | "reject") => {
    setBusy(true);
    try { await post(`/remediation/runs/${run.id}/${verb}`, verb === "reject" ? { reason: "Rejected from dashboard" } : undefined); }
    finally { setBusy(false); onDone(); }
  };
  return (
    <div className="card border-amber-500/30">
      <div className="flex items-center justify-between">
        <div><div className="font-medium text-slate-100">{run.title}</div>
          <Link to={`/devices/${run.device_id}`} className="text-sm text-brand-400">{run.device}</Link></div>
        {run.risk && <Badge value={run.risk} />}
      </div>
      <Gates run={run} />
      {can("operator") ? (
        <div className="mt-3 flex gap-2">
          <button className="btn-primary" disabled={busy} onClick={() => act("approve")}><Check className="h-4 w-4" /> Approve & run</button>
          <button className="btn-ghost" disabled={busy} onClick={() => act("reject")}><X className="h-4 w-4" /> Reject</button>
        </div>
      ) : <div className="mt-3 text-xs text-slate-500">Operator role required to approve.</div>}
      <div className="mt-2 text-xs text-slate-600">Requested {ago(run.created_at)} by {run.requested_by}</div>
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  const [open, setOpen] = useState(run.status === "running");
  return (
    <li>
      <button className="flex w-full items-center gap-3 p-3 text-left hover:bg-slate-800/40" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
        <div className="min-w-0 flex-1">
          <div className="text-sm text-slate-200"><b className="font-medium">{run.title}</b> on {run.device}</div>
          <div className="truncate text-xs text-slate-500">{ago(run.created_at)} · approved by {run.approved_by ?? "-"} · driver {run.driver ?? "-"}
            {run.reason && ` · ${run.reason}`}</div>
        </div>
        <Badge value={run.status} />
      </button>
      {open && (
        <div className="grid gap-4 border-t border-slate-800 bg-slate-950/40 p-4 md:grid-cols-2">
          <div>
            <div className="text-xs font-semibold uppercase text-slate-500">Steps</div>
            {run.steps.length === 0 ? <div className="mt-2 text-xs text-slate-500">Not executed</div> : (
              <ol className="mt-2 space-y-1.5 text-xs">
                {run.steps.map((s, i) => (
                  <li key={i} className="flex gap-2">
                    <span className={cx("mt-1 h-2 w-2 shrink-0 rounded-full", s.status === "ok" ? "bg-emerald-400" : s.status === "escalated" ? "bg-orange-400" : "bg-rose-500")} />
                    <div><span className={s.phase === "rollback" ? "text-orange-300" : "text-slate-200"}>{s.phase === "rollback" && "↩ "}{s.action}</span>
                      <div className="font-mono text-[11px] text-slate-500">{JSON.stringify(s.result)}</div></div>
                  </li>
                ))}
              </ol>
            )}
          </div>
          <div><div className="text-xs font-semibold uppercase text-slate-500">Safety gates</div><Gates run={run} /></div>
        </div>
      )}
    </li>
  );
}
