import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import type { Topology } from "../api";

export function HealthGauge({ score, size = 160 }: { score: number | null; size?: number }) {
  const r = size / 2 - 12;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, score ?? 0));
  const color = pct >= 95 ? "#34d399" : pct >= 85 ? "#fbbf24" : "#fb7185";
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`Health ${pct}%`}>
      <circle cx={size / 2} cy={size / 2} r={r} stroke="#1e293b" strokeWidth={12} fill="none" />
      <circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth={12} fill="none" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c * (1 - pct / 100)} transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset 1s ease, stroke 1s" }} />
      <text x="50%" y="48%" textAnchor="middle" fill="#f1f5f9" fontSize={size / 5} fontWeight={600}>
        {score === null ? "--" : `${pct.toFixed(1)}`}
      </text>
      <text x="50%" y="64%" textAnchor="middle" fill="#94a3b8" fontSize={size / 14}>health score</text>
    </svg>
  );
}

const time = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dateTime = (t: number) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" });

export function TrendChart({ data, color = "#38bdf8", unit = "", height = 180, domain, long = false }: {
  data: { t: number; v: number }[]; color?: string; unit?: string; height?: number;
  domain?: [number, number]; long?: boolean;
}) {
  const id = useMemo(() => `g${Math.random().toString(36).slice(2)}`, []);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 0, left: -18 }}>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#1e293b" vertical={false} />
        <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time"
          tickFormatter={long ? dateTime : time} stroke="#475569" fontSize={11} minTickGap={40} />
        <YAxis stroke="#475569" fontSize={11} domain={domain ?? [(min: number) => Math.max(0, Math.floor(min)), "auto"]} />
        <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: 8 }}
          labelFormatter={(t) => new Date(t as number).toLocaleString()}
          separator="" formatter={(v: number) => [`${v.toFixed(2)}${unit}`, ""]} />
        <Area type="monotone" dataKey="v" stroke={color} strokeWidth={2} fill={`url(#${id})`} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

const STATUS_COLOR: Record<string, string> = { up: "#34d399", degraded: "#fbbf24", down: "#fb7185", unknown: "#64748b" };
const TYPE_GLYPH: Record<string, string> = {
  router: "R", firewall: "F", switch: "S", access_point: "AP", server: "SV", cloud: "C",
};

/** Layered dependency graph: upstream devices on top, dependents below. */
export function TopologyMap({ topo, height = 460, highlight }: { topo: Topology; height?: number; highlight?: string }) {
  const navigate = useNavigate();
  const layout = useMemo(() => {
    const primary = topo.edges.filter((e) => e.kind === "primary");
    const parents = new Map<string, string[]>();
    primary.forEach((e) => parents.set(e.target, [...(parents.get(e.target) ?? []), e.source]));
    const depth = new Map<string, number>();
    const visit = (id: string, seen = new Set<string>()): number => {
      if (depth.has(id)) return depth.get(id)!;
      if (seen.has(id)) return 0;
      seen.add(id);
      const ps = parents.get(id) ?? [];
      const d = ps.length ? Math.max(...ps.map((p) => visit(p, seen))) + 1 : 0;
      depth.set(id, d);
      return d;
    };
    topo.nodes.forEach((n) => visit(n.id));
    const layers: string[][] = [];
    // Order each layer by parent position so edges don't cross much.
    const order = new Map<string, number>();
    [...topo.nodes].sort((a, b) => depth.get(a.id)! - depth.get(b.id)!).forEach((n) => {
      const d = depth.get(n.id)!;
      (layers[d] ??= []).push(n.id);
    });
    layers.forEach((layer, d) => {
      if (d > 0) {
        layer.sort((a, b) => {
          const pa = Math.min(...(parents.get(a) ?? [a]).map((p) => order.get(p) ?? 0));
          const pb = Math.min(...(parents.get(b) ?? [b]).map((p) => order.get(p) ?? 0));
          return pa - pb;
        });
      }
      layer.forEach((id, i) => order.set(id, i / Math.max(1, layer.length)));
    });
    const W = 1000;
    const H = Math.max(1, layers.length - 1);
    const pos = new Map<string, { x: number; y: number }>();
    layers.forEach((layer, d) => {
      const y = 40 + (d / H) * (height - 80);
      const gap = Math.min(W / layer.length, 115);
      // Barycentre: place children under the mean x of their parents, then
      // push apart to keep a minimum gap and pull back inside the canvas.
      const want = layer.map((id, i) => {
        const ps = (parents.get(id) ?? []).map((p) => pos.get(p)?.x).filter((x): x is number => x !== undefined);
        return { id, x: d > 0 && ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : ((i + 0.5) / layer.length) * W };
      }).sort((a, b) => a.x - b.x);
      for (let i = 1; i < want.length; i++) want[i].x = Math.max(want[i].x, want[i - 1].x + gap);
      const overflow = want.length ? want[want.length - 1].x - (W - gap / 2) : 0;
      if (overflow > 0) want.forEach((w) => (w.x -= overflow));
      for (let i = 0; i < want.length; i++) want[i].x = Math.max(want[i].x, gap / 2 + i * gap);
      want.forEach((w) => pos.set(w.id, { x: w.x, y }));
    });
    // Stretch the drawing horizontally to use the full canvas.
    const xs = [...pos.values()].map((p) => p.x);
    const [lo, hi] = [Math.min(...xs), Math.max(...xs)];
    if (hi > lo) pos.forEach((p) => (p.x = 60 + ((p.x - lo) / (hi - lo)) * (W - 120)));
    return { pos, W };
  }, [topo, height]);

  return (
    <svg viewBox={`0 0 ${layout.W} ${height}`} className="w-full" style={{ height }}>
      {topo.edges.map((e) => {
        const a = layout.pos.get(e.source), b = layout.pos.get(e.target);
        if (!a || !b) return null;
        return <line key={`${e.source}-${e.target}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
          stroke={e.kind === "backup" ? "#334155" : "#475569"} strokeDasharray={e.kind === "backup" ? "4 4" : undefined}
          strokeWidth={1.5} />;
      })}
      {topo.nodes.map((n) => {
        const p = layout.pos.get(n.id)!;
        const color = STATUS_COLOR[n.status] ?? STATUS_COLOR.unknown;
        return (
          <g key={n.id} transform={`translate(${p.x},${p.y})`} className="cursor-pointer" onClick={() => navigate(`/devices/${n.id}`)}>
            {(n.status === "down" || n.status === "degraded") && (
              <circle r={22} fill={color} opacity={0.25}><animate attributeName="r" values="16;26;16" dur="1.8s" repeatCount="indefinite" /></circle>
            )}
            <circle r={16} fill="#0f172a" stroke={highlight === n.id ? "#38bdf8" : color} strokeWidth={highlight === n.id ? 4 : 2.5} />
            <text textAnchor="middle" dy={4} fontSize={10} fontWeight={700} fill={color}>{TYPE_GLYPH[n.type] ?? "?"}</text>
            <text textAnchor="middle" dy={32} fontSize={11} fill="#94a3b8">{n.name}</text>
          </g>
        );
      })}
    </svg>
  );
}
