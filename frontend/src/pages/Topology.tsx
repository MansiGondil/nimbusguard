import type { Topology as Topo } from "../api";
import { useApi } from "../live";
import { TopologyMap } from "../components/charts";
import { Empty, PageHeader } from "../components/ui";

export default function Topology() {
  const { data } = useApi<Topo>("/topology", ["health"]);
  return (
    <>
      <PageHeader title="Topology" subtitle="Dependency map used for root-cause analysis and impact checks. Click a device to drill in." />
      <div className="card overflow-x-auto">
        {data ? <div className="min-w-[760px]"><TopologyMap topo={data} height={520} /></div> : <Empty>Loading...</Empty>}
        <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-500">
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-emerald-400" />up</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-amber-400" />degraded</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-rose-400" />down</span>
          <span>R router · F firewall · S switch · AP access point · SV server · C cloud · dashed = backup link</span>
        </div>
      </div>
    </>
  );
}
