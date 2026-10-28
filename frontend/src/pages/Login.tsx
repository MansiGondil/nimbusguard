import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";
import { Logo } from "../components/Layout";

export default function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full items-center justify-center bg-[radial-gradient(ellipse_at_top,_#0c4a6e_0%,_#020617_60%)] p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-6">
        <Logo />
        <p className="text-sm text-slate-400">
          AI-powered monitoring and self-healing for networks that don't have a full-time IT team.
        </p>
        <label className="block text-sm">
          <span className="text-slate-400">Username</span>
          <input className="input mt-1" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        </label>
        <label className="block text-sm">
          <span className="text-slate-400">Password</span>
          <input className="input mt-1" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" placeholder="demo: nimbusguard" />
        </label>
        {error && <div className="rounded-lg bg-rose-500/10 px-3 py-2 text-sm text-rose-300">{error}</div>}
        <button className="btn-primary w-full justify-center py-2" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</button>
      </form>
    </div>
  );
}
