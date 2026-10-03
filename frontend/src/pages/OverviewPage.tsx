import {
  Activity, ArrowRight, Building2, CirclePlay, IndianRupee, Landmark, Receipt, Sparkles, Users, Wallet,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, getOverview, type Overview, type TrendPoint } from "../lib/api";
import { formatINR } from "../lib/format";
import type { PageId } from "../lib/nav";
import type { User } from "../lib/types";
import { useToast } from "../components/layout/Toast";

// Display only: every amount below is a stored, processed payroll value from the API.

const ACTIVITY_LABEL: Record<string, string> = {
  "payroll.run": "Payroll processed",
  "batch.start": "Payroll review started",
  "batch.run": "Payroll run started",
  "batch.retry": "Failed employees retried",
  "batch.validate": "Payroll validated",
  "batch.approve": "Payroll approved",
  "batch.finalize": "Payroll finalized",
  "batch.rollback": "Payroll rolled back",
  "payslip.download": "Payslip downloaded",
  "batch.recalculate": "Month recalculation started",
  "batch.stage_reset": "Validation reset after a change",
  "action.submit": "Change requested",
  "action.approve": "Change approved",
  "action.reject": "Change rejected",
  "action.cancel": "Change withdrawn",
  "action.apply": "Change applied",
  "action.failed": "Change failed",
  "lop.update": "LOP updated",
  "overtime.add": "Overtime added",
  "overtime.cancel": "Overtime cancelled",
  "adjustment.add": "One-off pay added",
  "adjustment.cancel": "One-off pay cancelled",
  "salary.revise": "Salary revised",
};

const BATCH_LABEL: Record<string, string> = {
  REVIEW: "LOP review", QUEUED: "Queued", RUNNING: "Running", COMPLETED: "Calculated",
  COMPLETED_WITH_ERRORS: "Calculated with errors", CANCELLED: "Cancelled",
};

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-2xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>{children}</div>;
}

function Kpi({ icon: Icon, label, value, hint, tone = "indigo" }: {
  icon: LucideIcon; label: string; value: string; hint?: string; tone?: "indigo" | "emerald" | "amber" | "sky";
}) {
  const tones = {
    indigo: "bg-indigo-50 text-indigo-600", emerald: "bg-emerald-50 text-emerald-600",
    amber: "bg-amber-50 text-amber-600", sky: "bg-sky-50 text-sky-600",
  };
  return (
    <Card>
      <div className="flex items-start justify-between">
        <p className="text-xs font-medium text-slate-500">{label}</p>
        <span className={`flex h-8 w-8 items-center justify-center rounded-lg ${tones[tone]}`}><Icon className="h-4 w-4" /></span>
      </div>
      <p className="mt-2 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </Card>
  );
}

function Skeleton() {
  return (
    <div className="grid animate-pulse gap-4 p-4 sm:p-6 md:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className={`rounded-2xl bg-slate-200/70 ${i < 4 ? "h-28" : "h-56 md:col-span-2"}`} />
      ))}
    </div>
  );
}

/** Bars scaled to the largest month (a drawing ratio, not a payroll calculation). */
function TrendChart({ trend }: { trend: TrendPoint[] }) {
  const max = Math.max(...trend.map((t) => Number(t.gross)), 1);
  if (!trend.length) return <p className="py-10 text-center text-sm text-slate-400">No payroll processed yet.</p>;
  return (
    <div className="flex h-48 gap-3 pt-4">
      {trend.map((t) => (
        <div key={t.period} className="group flex flex-1 flex-col items-center gap-2">
          <div className="relative flex w-full max-w-14 flex-1 items-end">
            <div className="flex w-full flex-col justify-end rounded-t-lg bg-indigo-100" style={{ height: `${(Number(t.gross) / max) * 100}%` }}>
              <div className="w-full rounded-t-lg bg-indigo-500"
                style={{ height: `${(Number(t.net) / Number(t.gross || 1)) * 100}%` }} />
            </div>
            <span className="pointer-events-none absolute -top-7 left-1/2 hidden -translate-x-1/2 whitespace-nowrap rounded bg-slate-900 px-2 py-0.5 text-[11px] text-white group-hover:block">
              Net {formatINR(t.net)}
            </span>
          </div>
          <span className="text-[11px] text-slate-500">{t.label}</span>
        </div>
      ))}
    </div>
  );
}

function AskCard({ prompts, onAsk }: { prompts: string[]; onAsk: (text: string) => void }) {
  return (
    <Card className="bg-gradient-to-br from-indigo-600 to-violet-600 text-white">
      <div className="flex items-center gap-2"><Sparkles className="h-4 w-4" /><p className="text-sm font-semibold">Ask PayrollAI</p></div>
      <p className="mt-1 text-xs text-indigo-100">Answers come from payroll data, never guessed.</p>
      <div className="mt-4 flex flex-col gap-2">
        {prompts.map((p) => (
          <button key={p} type="button" onClick={() => onAsk(p)}
            className="flex items-center justify-between rounded-lg bg-white/10 px-3 py-2 text-left text-xs hover:bg-white/20">
            {p} <ArrowRight className="h-3.5 w-3.5 shrink-0" />
          </button>
        ))}
      </div>
    </Card>
  );
}

export function OverviewPage({ token, user, prompts, onAsk, onNavigate, onExpired }: {
  token: string; user: User; prompts: string[]; onAsk: (text: string) => void;
  onNavigate: (id: PageId) => void; onExpired: () => void;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    getOverview(token).then(setData).catch((e) => {
      if (e instanceof ApiError && e.status === 401) return onExpired();
      const message = e instanceof ApiError ? e.message : "Could not load the overview";
      setError(message);
      toast(message, "error");
    });
  }, [token, onExpired, toast]);

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  if (!data) return <Skeleton />;
  const first = user.full_name.split(" ")[0];

  if (data.scope === "personal") {
    const last = data.trend.at(-1);
    return (
      <div className="space-y-4 p-4 sm:p-6">
        <div>
          <h2 className="text-xl font-semibold text-slate-900">Hello, {first}</h2>
          <p className="text-sm text-slate-500">Your payroll at a glance.</p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Kpi icon={Wallet} label={`Net pay${last ? ` · ${last.label}` : ""}`} value={last ? formatINR(last.net) : "—"} tone="emerald" />
          <Kpi icon={IndianRupee} label="Gross earnings" value={last ? formatINR(last.gross) : "—"} />
          <Kpi icon={Receipt} label="Deductions" value={last ? formatINR(last.deductions) : "—"} tone="amber"
            hint={last ? `LOP days: ${Number(last.lop_days)}` : undefined} />
          {data.team_size != null
            ? <Kpi icon={Users} label="Your team" value={String(data.team_size)} tone="sky" hint="Profiles only, no salary amounts" />
            : <Kpi icon={Users} label="Months on record" value={String(data.trend.length)} tone="sky" />}
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <p className="text-sm font-semibold text-slate-900">Your pay trend</p>
            <p className="text-xs text-slate-400">Light bar: gross · dark bar: net pay</p>
            <TrendChart trend={data.trend} />
          </Card>
          <AskCard prompts={prompts} onAsk={onAsk} />
        </div>
      </div>
    );
  }

  const latest = data.latest_payroll;
  const batch = data.latest_batch;
  const maxDept = Math.max(...(data.departments ?? []).map((d) => d.count), 1);
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-slate-900">Hello, {first}</h2>
          <p className="text-sm text-slate-500">
            {latest ? `Latest processed payroll: ${latest.label} · ${latest.employees} employees` : "No payroll processed yet."}
          </p>
        </div>
        <button type="button" onClick={() => onNavigate("payroll-runs")}
          className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700">
          <CirclePlay className="h-4 w-4" /> Payroll Runs
        </button>
      </div>

      {!!data.pending_actions && (
        <button type="button" onClick={() => onNavigate("actions")}
          className="flex w-full items-center justify-between rounded-2xl border border-amber-200 bg-amber-50 px-5 py-3 text-left text-sm text-amber-900 hover:bg-amber-100">
          <span><strong>{data.pending_actions}</strong> payroll change{data.pending_actions === 1 ? "" : "s"} waiting for approval</span>
          <span className="inline-flex items-center gap-1 text-xs font-medium">Open Action Center <ArrowRight className="h-3.5 w-3.5" /></span>
        </button>
      )}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi icon={Users} label="Active employees" value={String(data.headcount!.active)} tone="sky"
          hint={`${data.headcount!.notice} on notice`} />
        <Kpi icon={IndianRupee} label={`Gross${latest ? ` · ${latest.label}` : ""}`} value={latest ? formatINR(latest.gross) : "—"} />
        <Kpi icon={Receipt} label="Deductions" value={latest ? formatINR(latest.deductions) : "—"} tone="amber" />
        <Kpi icon={Wallet} label="Net pay" value={latest ? formatINR(latest.net) : "—"} tone="emerald" />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <p className="text-sm font-semibold text-slate-900">Payroll trend</p>
          <p className="text-xs text-slate-400">Light bar: gross · dark bar: net pay (hover for amount)</p>
          <TrendChart trend={data.trend} />
        </Card>
        <AskCard prompts={prompts} onAsk={onAsk} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <div className="flex items-center gap-2"><Landmark className="h-4 w-4 text-slate-400" />
            <p className="text-sm font-semibold text-slate-900">Statutory deductions</p></div>
          <p className="text-xs text-slate-400">Employee share{latest ? ` · ${latest.label}` : ""}</p>
          <dl className="mt-4 space-y-3">
            {Object.entries(data.statutory ?? {}).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between text-sm">
                <dt className="text-slate-600">{k}</dt><dd className="font-medium tabular-nums text-slate-900">{formatINR(v)}</dd>
              </div>
            ))}
          </dl>
          {batch && (
            <div className="mt-5 rounded-xl bg-slate-50 p-3 text-xs">
              <p className="font-medium text-slate-700">Latest batch · {batch.period}</p>
              <p className="mt-0.5 text-slate-500">
                {BATCH_LABEL[batch.status] ?? batch.status}{batch.stage ? ` · ${batch.stage.toLowerCase()}` : ""} · {batch.total} employees
              </p>
            </div>
          )}
        </Card>

        <Card>
          <div className="flex items-center gap-2"><Building2 className="h-4 w-4 text-slate-400" />
            <p className="text-sm font-semibold text-slate-900">Headcount by department</p></div>
          <ul className="mt-4 space-y-2.5">
            {(data.departments ?? []).map((d) => (
              <li key={d.name} className="text-xs">
                <div className="flex justify-between text-slate-600"><span>{d.name}</span><span className="font-medium">{d.count}</span></div>
                <div className="mt-1 h-1.5 rounded-full bg-slate-100">
                  <div className="h-1.5 rounded-full bg-indigo-500" style={{ width: `${(d.count / maxDept) * 100}%` }} />
                </div>
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <div className="flex items-center gap-2"><Activity className="h-4 w-4 text-slate-400" />
            <p className="text-sm font-semibold text-slate-900">Recent activity</p></div>
          {data.activity?.length ? (
            <ol className="mt-4 space-y-3">
              {data.activity.map((a, i) => (
                <li key={i} className="flex gap-3 text-xs">
                  <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-indigo-400" />
                  <div className="min-w-0">
                    <p className="font-medium text-slate-700">{ACTIVITY_LABEL[a.action] ?? a.action}</p>
                    <p className="truncate text-slate-400">
                      {a.actor} · {new Date(a.at).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          ) : <p className="mt-6 text-center text-xs text-slate-400">No payroll activity yet.</p>}
        </Card>
      </div>
    </div>
  );
}
