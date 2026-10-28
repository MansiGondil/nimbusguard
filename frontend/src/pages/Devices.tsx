import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";
import type { Device } from "../api";
import { useApi, useLive } from "../live";
import { Badge, Empty, Meter, PageHeader, fmtMetric } from "../components/ui";

export default function Devices() {
  const { data } = useApi<Device[]>("/devices", ["health"]);
  const { metrics } = useLive();
  const [q, setQ] = useState("");
  const [type, setType] = useState("all");

  const rows = useMemo(() => (data ?? []).filter((d) =>
    (type === "all" || d.type === type) && (d.name + (d.ip ?? "") + d.site).toLowerCase().includes(q.toLowerCase())), [data, q, type]);
  const types = [...new Set((data ?? []).map((d) => d.type))];

  return (
    <>
      <PageHeader title="Devices" subtitle={`${data?.length ?? 0} monitored devices`} />
      <div className="mb-4 flex flex-wrap gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
          <input className="input pl-9" placeholder="Search name, IP, site" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <select className="input w-auto" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="all">All types</option>
          {types.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
        </select>
      </div>
      <div className="card overflow-x-auto p-0">
        {!data ? <Empty>Loading...</Empty> : (
          <table className="table">
            <thead>
              <tr><th>Device</th><th>Status</th><th>Type</th><th>Criticality</th><th className="w-32">CPU</th>
                <th className="w-32">Memory</th><th>Traffic in</th><th>Latency</th><th>Loss</th></tr>
            </thead>
            <tbody>
              {rows.map((d) => {
                const m = metrics[d.id] ?? d.metrics;
                return (
                  <tr key={d.id} className="hover:bg-slate-800/40">
                    <td>
                      <Link to={`/devices/${d.id}`} className="font-medium text-slate-100 hover:text-brand-400">{d.name}</Link>
                      <div className="text-xs text-slate-500">{d.ip} · {d.site}{d.maintenance_mode && " · maintenance"}</div>
                    </td>
                    <td><Badge value={d.status} /></td>
                    <td className="text-slate-400">{d.type.replace("_", " ")}</td>
                    <td><Badge value={d.criticality === "critical" ? "high" : d.criticality} /></td>
                    <td><div className="mb-1 text-xs tabular-nums">{fmtMetric("cpu", m.cpu)}</div><Meter value={m.cpu} /></td>
                    <td><div className="mb-1 text-xs tabular-nums">{fmtMetric("memory", m.memory)}</div><Meter value={m.memory} /></td>
                    <td className="tabular-nums">{fmtMetric("traffic_in", m.traffic_in)}</td>
                    <td className="tabular-nums">{fmtMetric("latency", m.latency)}</td>
                    <td className="tabular-nums">{fmtMetric("packet_loss", m.packet_loss)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
