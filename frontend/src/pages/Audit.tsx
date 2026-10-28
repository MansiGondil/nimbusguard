import { useState } from "react";
import { Link2, ShieldCheck, ShieldX } from "lucide-react";
import type { AuditEntry } from "../api";
import { useApi } from "../live";
import { Empty, PageHeader } from "../components/ui";

export default function Audit() {
  const [filter, setFilter] = useState("");
  const { data } = useApi<AuditEntry[]>(`/audit?limit=300${filter ? `&action=${filter}` : ""}`, ["remediation", "incident"]);
  const { data: verify } = useApi<{ valid: boolean; entries: number; broken_at: number | null }>("/audit/verify", ["remediation"]);

  return (
    <>
      <PageHeader title="Audit log" subtitle="Every action, approval and login, hash-chained so tampering is detectable"
        actions={verify && (
          <div className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm ${verify.valid ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/10 text-rose-300"}`}>
            {verify.valid ? <ShieldCheck className="h-4 w-4" /> : <ShieldX className="h-4 w-4" />}
            {verify.valid ? `Chain intact · ${verify.entries} entries` : `Chain broken at #${verify.broken_at}`}
          </div>)} />
      <div className="mb-3 flex gap-2">
        {["", "remediation", "incident", "auth", "device", "simulator", "user"].map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`rounded-md px-2.5 py-1 text-xs ${filter === f ? "bg-brand-600 text-white" : "bg-slate-800 text-slate-400 hover:text-slate-200"}`}>
            {f || "all"}</button>))}
      </div>
      <div className="card overflow-x-auto p-0">
        {!data ? <Empty>Loading...</Empty> : (
          <table className="table">
            <thead><tr><th>#</th><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th><th>Hash</th></tr></thead>
            <tbody>
              {data.map((e) => (
                <tr key={e.id}>
                  <td className="text-slate-500">{e.id}</td>
                  <td className="whitespace-nowrap text-xs">{new Date(e.time).toLocaleString()}</td>
                  <td>{e.actor}</td>
                  <td className="font-mono text-xs text-sky-300">{e.action}</td>
                  <td>{e.target}</td>
                  <td className="max-w-xs truncate font-mono text-[11px] text-slate-500" title={JSON.stringify(e.detail)}>{JSON.stringify(e.detail)}</td>
                  <td className="font-mono text-[11px] text-slate-600"><Link2 className="mr-1 inline h-3 w-3" />{e.hash.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
