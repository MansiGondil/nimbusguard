import { useState } from "react";
import { FlaskConical, Play, Square } from "lucide-react";
import { post, type Device, type Fault } from "../api";
import { useAuth } from "../auth";
import { useApi } from "../live";
import { Empty, PageHeader, ago } from "../components/ui";

interface SimState { enabled: boolean; scenarios: Record<string, { label: string; types: string[] }>; faults: Fault[] }

const STORY: Record<string, string> = {
  cpu_spike: "A runaway process pins CPU. After the threshold holds, the high-cpu playbook restarts it.",
  memory_leak: "Memory climbs steadily. The predictor forecasts exhaustion and restarts the service before it crashes.",
  bad_cable: "CRC errors and packet loss spread to every device downstream. RCA finds the real culprit.",
  link_saturation: "The WAN link fills up. Latency rises across the network.",
  disk_full: "Logs fill the disk. Cleanup archives old files automatically.",
  brute_force: "Thousands of failed logins hit the firewall. The attacker IPs get blocked.",
  exfiltration: "A host uploads gigabytes to an unknown server. Quarantine needs human approval.",
  device_down: "A device stops responding. Its dependents become unreachable too.",
};

export default function Simulator() {
  const { can } = useAuth();
  const { data, reload } = useApi<SimState>("/simulator", ["simulator"]);
  const { data: devices } = useApi<Device[]>("/devices");
  const [target, setTarget] = useState<Record<string, string>>({});
  const [stubborn, setStubborn] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  if (!data) return <Empty>Loading...</Empty>;
  if (!data.enabled) return <Empty>The simulator is disabled (NG_SIMULATOR_ENABLED=false).</Empty>;

  const inject = async (scenario: string) => {
    const res = await post<{ device_id: string }>("/simulator/inject", { scenario, device_id: target[scenario] || null, stubborn });
    const name = devices?.find((d) => d.id === res.device_id)?.name;
    setMsg(`Injected ${data.scenarios[scenario].label} on ${name}. Watch Operations and Incidents.`);
    reload();
  };
  const clear = async () => { await post("/simulator/clear", {}); setMsg("All faults cleared"); reload(); };

  return (
    <>
      <PageHeader title="Fault simulator" subtitle="Inject realistic failures into the simulated network and watch NimbusGuard detect, diagnose and heal them."
        actions={can("operator") && <button className="btn-danger" onClick={clear}><Square className="h-4 w-4" /> Clear all faults</button>} />
      {msg && <div className="mb-4 rounded-lg bg-brand-600/10 px-4 py-2 text-sm text-brand-400">{msg}</div>}
      <div className="card mb-4">
        <div className="card-title"><FlaskConical className="h-4 w-4" /> Active faults</div>
        {data.faults.length === 0 ? <Empty>Network is running normally</Empty> : (
          <ul className="text-sm">{data.faults.map((f) => (
            <li key={f.kind + f.device_id} className="py-1"><b>{data.scenarios[f.kind]?.label}</b> on {f.device} · {ago(f.started)}
              {f.stubborn && <span className="ml-2 text-xs text-orange-300">(stubborn: fixes will fail and roll back)</span>}</li>))}</ul>
        )}
      </div>
      {can("operator") && (
        <label className="mb-3 flex items-center gap-2 text-sm text-slate-400">
          <input type="checkbox" checked={stubborn} onChange={(e) => setStubborn(e.target.checked)} />
          Stubborn fault: the automated fix won't work, so you can see verification fail and the rollback run
        </label>
      )}
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
        {Object.entries(data.scenarios).map(([key, s]) => (
          <div key={key} className="card flex flex-col">
            <div className="font-medium text-slate-100">{s.label}</div>
            <p className="mt-1 flex-1 text-xs text-slate-400">{STORY[key]}</p>
            <select className="input mt-3 text-xs" value={target[key] ?? ""} onChange={(e) => setTarget({ ...target, [key]: e.target.value })}>
              <option value="">Random {s.types.join("/")}</option>
              {devices?.filter((d) => s.types.includes(d.type)).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
            <button className="btn-primary mt-2 justify-center" disabled={!can("operator")} onClick={() => inject(key)}>
              <Play className="h-4 w-4" /> Inject</button>
          </div>
        ))}
      </div>
    </>
  );
}
