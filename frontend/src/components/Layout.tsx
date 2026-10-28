import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  Activity, BarChart3, FlaskConical, LogOut, Menu, Network, ScrollText, Server, ShieldAlert, Wrench, X,
} from "lucide-react";
import { useAuth } from "../auth";
import { useLive } from "../live";
import { cx } from "./ui";

const NAV = [
  { to: "/", label: "Executive", icon: BarChart3, end: true },
  { to: "/operations", label: "Operations", icon: Activity },
  { to: "/devices", label: "Devices", icon: Server },
  { to: "/incidents", label: "Incidents", icon: ShieldAlert },
  { to: "/remediation", label: "Remediation", icon: Wrench },
  { to: "/topology", label: "Topology", icon: Network },
  { to: "/simulator", label: "Simulator", icon: FlaskConical },
  { to: "/audit", label: "Audit log", icon: ScrollText, role: "admin" as const },
];

export function Logo() {
  return (
    <div className="flex items-center gap-2">
      <img src="/favicon.svg" alt="" className="h-8 w-8" />
      <div>
        <div className="text-lg font-semibold leading-none text-slate-100">NimbusGuard</div>
        <div className="whitespace-nowrap text-[9px] uppercase tracking-wider text-brand-400">Predict · Prevent · Protect</div>
      </div>
    </div>
  );
}

export default function Layout() {
  const { user, logout, can } = useAuth();
  const { connected, health, toasts, dismiss } = useLive();
  const [open, setOpen] = useState(false);

  const nav = (
    <nav className="flex flex-col gap-1">
      {NAV.filter((n) => !n.role || can(n.role)).map(({ to, label, icon: Icon, end }) => (
        <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)}
          className={({ isActive }) => cx("flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition",
            isActive ? "bg-brand-600/20 text-brand-400" : "text-slate-400 hover:bg-slate-800 hover:text-slate-200")}>
          <Icon className="h-4 w-4" /> {label}
        </NavLink>
      ))}
    </nav>
  );

  return (
    <div className="flex h-full">
      <aside className={cx("fixed inset-y-0 left-0 z-40 w-60 flex-col border-r border-slate-800 bg-slate-950 p-4 lg:static lg:flex",
        open ? "flex" : "hidden")}>
        <div className="mb-8 flex items-center justify-between"><Logo />
          <button className="lg:hidden" onClick={() => setOpen(false)} aria-label="Close menu"><X className="h-5 w-5" /></button>
        </div>
        {nav}
        <div className="mt-auto space-y-3 border-t border-slate-800 pt-4 text-xs text-slate-500">
          <div className="flex items-center gap-2">
            <span className={cx("h-2 w-2 rounded-full", connected ? "bg-emerald-400" : "bg-rose-500")} />
            {connected ? "Live stream connected" : "Reconnecting..."}
          </div>
          {health !== null && <div>Network health <span className="font-semibold text-slate-300">{health}%</span></div>}
          <div className="flex items-center justify-between">
            <span>{user?.username} <span className="text-slate-600">({user?.role})</span></span>
            <button onClick={logout} className="text-slate-400 hover:text-slate-200" aria-label="Log out"><LogOut className="h-4 w-4" /></button>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="sticky top-0 z-30 flex items-center gap-3 border-b border-slate-800 bg-slate-950/90 px-4 py-3 backdrop-blur lg:hidden">
          <button onClick={() => setOpen(true)} aria-label="Open menu"><Menu className="h-5 w-5" /></button>
          <Logo />
        </div>
        <div className="mx-auto max-w-7xl p-4 md:p-8"><Outlet /></div>
      </main>
      <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <div key={t.id} onClick={() => dismiss(t.id)}
            className={cx("cursor-pointer rounded-lg border bg-slate-900/95 p-3 text-sm shadow-xl backdrop-blur",
              t.priority === "high" ? "border-rose-500/50" : "border-slate-700")}>
            <div className="font-semibold text-slate-100">{t.title}</div>
            <div className="mt-0.5 text-slate-400">{t.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
