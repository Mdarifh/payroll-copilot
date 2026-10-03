import { useCan } from "../lib/permissions";
import {
  CircleAlert, CircleCheck, Layers, Play, ScanSearch, Sparkles, TriangleAlert, Wrench,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/layout/Toast";
import {
  ApiError, auditRun, auditRuns, payrollMonths, recalculateEmployee, startAudit,
  type AuditIssue, type AuditRun, type PayrollMonth,
} from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import type { PageId } from "../lib/nav";

// Display only: findings, causes and the health score come from the backend audit.

const SEVERITY = {
  HIGH: "bg-rose-50 text-rose-700 ring-rose-200", MEDIUM: "bg-amber-50 text-amber-700 ring-amber-200",
  LOW: "bg-slate-100 text-slate-600 ring-slate-200",
};
const STATUS_ICON = {
  PASS: <CircleCheck className="h-4 w-4 text-emerald-600" />, WARNING: <TriangleAlert className="h-4 w-4 text-amber-500" />,
  FAIL: <CircleAlert className="h-4 w-4 text-rose-600" />,
};
const COMPONENT_WORD: Record<string, string> = { EE_PF: "PF", EE_ESIC: "ESIC", PT: "PT", TDS: "TDS", LOP_DAYS: "LOP", PAID_DAYS: "LOP", NET_PAY: "net pay", GROSS: "gross" };

const value = (v: string | null | undefined, unit?: string) => (v == null ? "—" : unit === "DAYS" ? `${formatDays(v)} d` : formatINR(v));

function Score({ score }: { score: number }) {
  const tone = score >= 95 ? "text-emerald-600" : score >= 80 ? "text-amber-500" : "text-rose-600";
  const r = 34, c = 2 * Math.PI * r;
  return (
    <div className="relative h-24 w-24">
      <svg viewBox="0 0 80 80" className="h-24 w-24 -rotate-90">
        <circle cx="40" cy="40" r={r} className="fill-none stroke-slate-100" strokeWidth="8" />
        <circle cx="40" cy="40" r={r} className={`fill-none stroke-current ${tone}`} strokeWidth="8" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - score / 100)} />
      </svg>
      <span className={`absolute inset-0 flex items-center justify-center text-xl font-semibold ${tone}`}>{score}%</span>
    </div>
  );
}

function IssueRow({ i, month, onAsk, onNavigate, onFix }: {
  i: AuditIssue; month: string; onAsk: (t: string) => void; onNavigate: (p: PageId) => void; onFix: (i: AuditIssue) => void;
}) {
  const can = useCan();
  const [open, setOpen] = useState(false);
  const word = COMPONENT_WORD[i.component ?? ""] ?? "net pay";
  return (
    <li className="py-3">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-start justify-between gap-2 text-left">
        <span className="min-w-0">
          <span className="text-sm font-semibold text-slate-900">{i.employee_name}</span>{" "}
          <span className="text-xs text-slate-400">{i.employee_code}</span>
          <span className="block text-xs text-slate-600">{i.issue}{i.message ? ` — ${i.message}` : ""}</span>
        </span>
        <span className="flex items-center gap-3 text-xs">
          {i.expected != null && (
            <span className="tabular-nums text-slate-600">expected {value(i.expected, i.unit)} · payslip {value(i.actual, i.unit)}</span>
          )}
          <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ${SEVERITY[i.severity]}`}>{i.severity}</span>
        </span>
      </button>
      {open && (
        <div className="mt-2 space-y-2 rounded-xl bg-slate-50 p-3 text-xs">
          {i.expected != null && (
            <div className="grid grid-cols-3 gap-2 text-center">
              <div><p className="text-[10px] uppercase text-slate-400">Expected</p><p className="font-semibold tabular-nums">{value(i.expected, i.unit)}</p></div>
              <div><p className="text-[10px] uppercase text-slate-400">Payslip</p><p className="font-semibold tabular-nums">{value(i.actual, i.unit)}</p></div>
              <div><p className="text-[10px] uppercase text-slate-400">Difference</p><p className="font-semibold tabular-nums text-rose-700">{value(i.difference, i.unit)}</p></div>
            </div>
          )}
          {i.causes?.length ? (
            <div><p className="font-semibold text-slate-700">Root cause</p>{i.causes.map((c) => <p key={c} className="text-slate-700">{c}</p>)}</div>
          ) : null}
          {i.effects?.length ? <p className="text-slate-500">Also affects: {i.effects.join(", ")}</p> : null}
          {i.recommendation && <p className="text-slate-500">Recommendation: {i.recommendation}</p>}
          <div className="flex flex-wrap gap-2 pt-1">
            <button type="button" onClick={() => onNavigate("employees")} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-100"><Layers className="h-3.5 w-3.5" />View salary structure</button>
            <button type="button" onClick={() => onAsk(`Explain ${i.employee_code} ${word} for ${month}`)} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-100"><Sparkles className="h-3.5 w-3.5" />Investigate</button>
            {i.can_fix && can("run_payroll") && (
              <button type="button" onClick={() => onFix(i)} className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-2.5 py-1 font-medium text-white hover:bg-indigo-700"><Wrench className="h-3.5 w-3.5" />Fix (recalculate payslip)</button>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export function AiAuditPage({ token, onAsk, onNavigate, onExpired }: {
  token: string; onAsk: (text: string) => void; onNavigate: (page: PageId) => void; onExpired: () => void;
}) {
  const toast = useToast();
  const [months, setMonths] = useState<PayrollMonth[]>([]);
  const [month, setMonth] = useState("");
  const [run, setRun] = useState<AuditRun | null>(null);
  const [history, setHistory] = useState<AuditRun[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [fixing, setFixing] = useState<AuditIssue | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onExpired();
    setError(e instanceof ApiError ? e.message : "Could not load the audit");
  }, [onExpired]);

  const loadHistory = useCallback(() => auditRuns(token).then((h) => {
    setHistory(h);
    return h;
  }).catch(fail), [token, fail]);

  useEffect(() => {
    payrollMonths(token).then((m) => {
      const processed = m.filter((x) => x.employees > 0);
      setMonths(processed);
      setMonth((cur) => cur || processed[0]?.period || "");
    }).catch(fail);
  }, [token, fail]);

  // open the latest run of the chosen month
  useEffect(() => {
    if (!month) return;
    void loadHistory().then((h) => {
      const last = (h ?? []).find((r) => r.period === month);
      if (last) auditRun(token, last.id).then(setRun).catch(fail); else setRun(null);
    });
  }, [month, token, loadHistory, fail]);

  // poll a running audit
  useEffect(() => {
    if (run?.status !== "RUNNING") return;
    const t = setInterval(() => {
      auditRun(token, run.id).then((r) => { setRun(r); if (r.status !== "RUNNING") void loadHistory(); }).catch(fail);
    }, 2000);
    return () => clearInterval(t);
  }, [run, token, loadHistory, fail]);

  async function start() {
    try {
      const r = await startAudit(token, month);
      setRun(r);
      setFilter("");
    } catch (e) { fail(e); }
  }

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  const issues = run?.status === "COMPLETED" ? [...(run.findings ?? []), ...(run.anomalies ?? [])] : [];
  const shown = issues.filter((i) => !filter || i.issue === filter)
    .sort((a, b) => ["HIGH", "MEDIUM", "LOW"].indexOf(a.severity) - ["HIGH", "MEDIUM", "LOW"].indexOf(b.severity));
  const s = run?.summary;

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><ScanSearch className="h-4 w-4 text-indigo-500" />AI Payroll Audit</p>
          <p className="text-xs text-slate-500">Checks every processed payslip against the payroll engine and finds anomalies. Read-only — nothing is changed.</p>
        </div>
        <div className="flex items-center gap-2">
          <select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month" className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm">
            {months.length === 0 && <option value="">No processed months</option>}
            {months.map((m) => <option key={m.period} value={m.period}>{m.label} · {m.employees} payslips</option>)}
          </select>
          <button type="button" onClick={start} disabled={!month || run?.status === "RUNNING"}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            <Play className="h-4 w-4" />{run?.status === "RUNNING" ? "Running…" : "Run AI audit"}
          </button>
        </div>
      </section>

      {!run && month && <p className="rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">No audit yet for this month. Press “Run AI audit”.</p>}

      {run?.status === "RUNNING" && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-medium text-slate-800">Auditing {run.period_label}… {run.checked} / {run.total || "…"} employees</p>
          <div className="mt-2 h-2 rounded-full bg-slate-100"><div className="h-2 rounded-full bg-indigo-500 transition-all" style={{ width: `${run.total ? (run.checked / run.total) * 100 : 5}%` }} /></div>
        </section>
      )}
      {run?.status === "FAILED" && <p className="rounded-xl bg-rose-50 p-4 text-sm text-rose-700">Audit failed: {run.error}</p>}

      {run?.status === "COMPLETED" && s && run.health && (
        <>
          <div className="grid gap-4 lg:grid-cols-3">
            <section className="flex items-center gap-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <Score score={run.health.score} />
              <div>
                <p className="text-sm font-semibold text-slate-900">Payroll health · {run.period_label}</p>
                <p className="mt-1 text-xs text-slate-500">{s.employees} employees · {s.payslips_checked} payslips checked</p>
                <p className="mt-2 text-xs"><span className="text-emerald-700">{s.passed} passed</span> · <span className="text-amber-700">{s.warnings} warnings</span> · <span className="text-rose-700">{s.failed} failed</span></p>
                <p className="mt-1 text-[11px] text-slate-400">{run.created_by} · {run.finished_at ? new Date(run.finished_at).toLocaleString("en-IN") : ""}</p>
              </div>
            </section>
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {run.health.categories.map((c) => (
                  <div key={c.category} className="flex items-center gap-2 rounded-xl border border-slate-100 px-3 py-2">
                    {STATUS_ICON[c.status]}
                    <div><p className="text-xs font-medium text-slate-800">{c.category}</p>
                      <p className="text-[11px] text-slate-500">{c.failed ? `${c.failed} failed` : c.warnings ? `${c.warnings} warning` : "OK"}</p></div>
                  </div>
                ))}
              </div>
              {Object.keys(s.statutory_impact).length > 0 && (
                <p className="mt-3 text-xs text-slate-600">
                  Payslips vs engine: {Object.entries(s.statutory_impact).map(([k, v]) => `${k} ${Number(v) > 0 ? "over" : "under"}-deducted by ${formatINR(String(Math.abs(Number(v))))}`).join(" · ")}
                </p>
              )}
            </section>
          </div>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => setFilter("")} className={`rounded-full px-3 py-1 text-xs font-medium ${!filter ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-700"}`}>All issues · {issues.length}</button>
              {Object.entries(s.by_issue).map(([k, n]) => (
                <button key={k} type="button" onClick={() => setFilter(k)} className={`rounded-full px-3 py-1 text-xs font-medium ${filter === k ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-700"}`}>{k} · {n}</button>
              ))}
            </div>
            {shown.length === 0 ? <p className="py-8 text-center text-sm text-emerald-700">No issues found. ✓</p> : (
              <ul className="mt-2 divide-y divide-slate-100">
                {shown.map((i, k) => <IssueRow key={`${i.employee_code}-${i.issue}-${k}`} i={i} month={run.period_label} onAsk={onAsk} onNavigate={onNavigate} onFix={setFixing} />)}
              </ul>
            )}
            <p className="mt-3 text-[11px] text-slate-400">{s.note} Causes are only shown when the data proves them.</p>
          </section>
        </>
      )}

      {history.length > 0 && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-semibold text-slate-900">Audit history</p>
          <ul className="mt-2 divide-y divide-slate-100 text-xs">
            {history.slice(0, 8).map((h) => (
              <li key={h.id} className="flex justify-between py-2">
                <button type="button" onClick={() => auditRun(token, h.id).then((r) => { setMonth(r.period); setRun(r); }).catch(fail)} className="text-left text-indigo-600 hover:underline">{h.period_label} · {h.started_at ? new Date(h.started_at).toLocaleString("en-IN") : ""}</button>
                <span className="text-slate-500">{h.status === "COMPLETED" ? `health ${h.health?.score}% · ${h.summary?.failed} failed` : h.status.toLowerCase()} · {h.created_by}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {fixing && run && (
        <ConfirmDialog title={`Fix ${fixing.employee_name}'s payslip`} confirmLabel="Recalculate payslip" reasonLabel="Reason (required)"
          facts={[{ label: "Employee", value: `${fixing.employee_name} (${fixing.employee_code})` }, { label: "Month", value: run.period_label },
            { label: "Finding", value: fixing.issue }, { label: "Expected", value: value(fixing.expected, fixing.unit) }, { label: "Payslip now", value: value(fixing.actual, fixing.unit) }]}
          warning="The payslip is calculated again with today's data and rules (version +1). A validated or approved month must be validated again."
          onClose={() => setFixing(null)}
          onConfirm={async (reason) => {
            const r = await recalculateEmployee(token, run.period, fixing.employee_code, reason);
            toast(`Recalculated: net ${formatINR(r.old_net)} → ${formatINR(r.new_net)}. Run the audit again to confirm.`, "success");
          }} />
      )}
    </div>
  );
}
