import { useState, type FormEvent } from "react";
import { ApiError, login } from "../lib/api";
import type { Session } from "../lib/types";
import { BotIcon, LockIcon } from "./icons";

const DEMO_USERS = [
  { label: "Employee - Rahul Verma", email: "rahul.verma@example.com", password: "Employee@123" },
  { label: "Employee - Priya Nair", email: "priya.nair@example.com", password: "Employee@123" },
  { label: "HR (admin) - Anjali Mehta", email: "anjali.mehta@example.com", password: "Hr@12345" },
  { label: "HR approver - Rohit Kapoor", email: "rohit.kapoor@example.com", password: "Hr@12345" },
  { label: "Payroll Manager - Kavita Rao", email: "kavita.rao@example.com", password: "Hr@12345" },
  { label: "Payroll Executive - Sameer Khan", email: "sameer.khan@example.com", password: "Hr@12345" },
  { label: "Viewer - Neha Gupta", email: "neha.gupta@example.com", password: "Hr@12345" },
];

export function LoginPage({ onLogin, notice }: { onLogin: (s: Session) => void; notice?: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(email, password));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-emerald-600 text-white">
            <BotIcon className="h-6 w-6" />
          </div>
          <h1 className="text-xl font-semibold text-slate-900">Payroll Assistant</h1>
          <p className="mt-1 text-sm text-slate-500">Sign in to ask about your pay, leave and tax</p>
        </div>

        <form onSubmit={submit} className="space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          {notice && <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">{notice}</p>}
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Email</span>
            <input type="email" required autoComplete="username" value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100" />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Password</span>
            <input type="password" required autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100" />
          </label>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button type="submit" disabled={busy}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-60">
            <LockIcon />
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div className="mt-5 rounded-2xl border border-dashed border-slate-300 p-4">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Demo logins</p>
          <div className="space-y-1.5">
            {DEMO_USERS.map((u) => (
              <button key={u.email} type="button"
                onClick={() => { setEmail(u.email); setPassword(u.password); setError(null); }}
                className="block w-full rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100">
                {u.label}
                <span className="block text-xs text-slate-400">{u.email}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </main>
  );
}
