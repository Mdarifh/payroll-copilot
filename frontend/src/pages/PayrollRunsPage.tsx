import { useCan } from "../lib/permissions";
import {
  BadgeCheck, CircleAlert, CircleCheck, CirclePlay, ClipboardCheck, Landmark, Lock, RefreshCw,
  RotateCcw, ScanSearch, ShieldCheck, TriangleAlert,
} from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { BulkLopCard } from "../components/BulkLopCard";
import { PayrollRegister } from "../components/PayrollRegister";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/layout/Toast";
import {
  ApiError, approveBatch, directoryFilters, finalizeBatch, payrollMonth, payrollMonths, payrollPrechecks,
  createMonthAttendance, pfChallan, recalculateMonth, reopenMonth, rollbackBatch, startBatch, validateBatch, validationResults,
  type Challan, type MonthDetail, type PayrollMonth, type Prechecks, type RunBatch, type ValidationPage,
} from "../lib/api";
import { formatINR } from "../lib/format";

// Display only: totals, checks and validation come from the backend; every write is confirmed.

const STATUS_STYLE: Record<string, { label: string; tone: string }> = {
  NOT_STARTED: { label: "Not started", tone: "bg-slate-100 text-slate-600" },
  PROCESSED: { label: "Processed", tone: "bg-sky-50 text-sky-700" },
  REVIEW: { label: "LOP review", tone: "bg-amber-50 text-amber-700" },
  QUEUED: { label: "Queued", tone: "bg-indigo-50 text-indigo-700" },
  RUNNING: { label: "Running", tone: "bg-indigo-50 text-indigo-700" },
  COMPLETED: { label: "Calculated", tone: "bg-sky-50 text-sky-700" },
  COMPLETED_WITH_ERRORS: { label: "Calculated with errors", tone: "bg-rose-50 text-rose-700" },
  CANCELLED: { label: "Cancelled", tone: "bg-slate-100 text-slate-600" },
  VALIDATED: { label: "Validated", tone: "bg-emerald-50 text-emerald-700" },
  VALIDATION_FAILED: { label: "Validation failed", tone: "bg-rose-50 text-rose-700" },
  APPROVED: { label: "Approved", tone: "bg-violet-50 text-violet-700" },
  FINALIZED: { label: "Finalized", tone: "bg-emerald-600 text-white" },
};
const STEPS = ["Pre-checks", "LOP review", "Calculate", "Validate", "Approve", "Finalize", "AI Audit"];
const ACTIVE = ["REVIEW", "QUEUED", "RUNNING"];

function Badge({ status }: { status: string }) {
  const s = STATUS_STYLE[status] ?? { label: status, tone: "bg-slate-100 text-slate-600" };
  return <span className={`whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${s.tone}`}>{s.label}</span>;
}

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-2xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>{children}</section>;
}

function stepIndex(batch: RunBatch | undefined): number {
  if (!batch || batch.status === "CANCELLED") return 0;
  if (batch.status === "REVIEW") return 1;
  if (!batch.finished) return 2;
  switch (batch.stage) {
    case "VALIDATED": return 4;
    case "APPROVED": return 5;
    case "FINALIZED": return 6;
    default: return 3; // calculated (or validation failed): validate next
  }
}

function Stepper({ current }: { current: number }) {
  return (
    <ol className="flex flex-wrap items-center gap-y-2">
      {STEPS.map((s, i) => (
        <li key={s} className="flex items-center">
          <span className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${i < current
            ? "bg-emerald-50 text-emerald-700" : i === current ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-500"}`}>
            {i < current ? <CircleCheck className="h-3.5 w-3.5" /> : <span className="text-[10px]">{i + 1}</span>}
            {s}
          </span>
          {i < STEPS.length - 1 && <span className="mx-1 h-px w-3 bg-slate-300 sm:w-5" />}
        </li>
      ))}
    </ol>
  );
}

function PrecheckGrid({ data }: { data: Prechecks }) {
  const [open, setOpen] = useState<string | null>(null);
  const icon = { PASS: <CircleCheck className="h-4 w-4 text-emerald-600" />, WARNING: <TriangleAlert className="h-4 w-4 text-amber-500" />,
    FAIL: <CircleAlert className="h-4 w-4 text-rose-600" /> };
  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {data.checks.map((c) => (
          <button key={c.key} type="button" onClick={() => setOpen(open === c.key ? null : c.key)}
            className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left ${open === c.key ? "border-indigo-300 bg-indigo-50/40" : "border-slate-200 hover:bg-slate-50"}`}>
            {icon[c.status]}
            <span className="flex-1">
              <span className="block text-sm font-medium text-slate-800">{c.label}</span>
              <span className="text-[11px] text-slate-500">{c.passed} ok{c.warnings ? ` · ${c.warnings} warning` : ""}{c.failed ? ` · ${c.failed} failed` : ""}</span>
            </span>
            {c.issue_count > 0 && <span className="text-[11px] text-indigo-600">Review</span>}
          </button>
        ))}
      </div>
      {open && (() => {
        const c = data.checks.find((x) => x.key === open)!;
        return (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
            {c.notes.map((n) => <p key={n.message} className={n.severity === "ERROR" ? "text-rose-700" : n.severity === "WARNING" ? "text-amber-700" : "text-slate-600"}>{n.message}</p>)}
            {c.issues.length === 0 && c.notes.length === 0 && <p className="text-slate-500">No issues.</p>}
            <ul className="mt-1 max-h-56 divide-y divide-slate-200 overflow-y-auto">
              {c.issues.map((i, k) => (
                <li key={k} className="flex gap-2 py-1.5">
                  {i.severity === "ERROR" ? <CircleAlert className="h-3.5 w-3.5 shrink-0 text-rose-600" /> : <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-amber-500" />}
                  <span><span className="font-medium text-slate-800">{i.employee_name}</span> <span className="text-slate-400">{i.employee_code}</span> — {i.message}</span>
                </li>
              ))}
            </ul>
            {c.issue_count > c.issues.length && <p className="mt-1 text-slate-400">Showing {c.issues.length} of {c.issue_count}.</p>}
          </div>
        );
      })()}
    </div>
  );
}

function ValidationList({ token, batchId, refreshKey }: { token: string; batchId: string; refreshKey: number }) {
  const [severity, setSeverity] = useState("error");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ValidationPage | null>(null);
  useEffect(() => { setPage(1); }, [severity, batchId]);
  useEffect(() => {
    let live = true;
    validationResults(token, batchId, severity, page).then((d) => { if (live) setData(d); }).catch(() => undefined);
    return () => { live = false; };
  }, [token, batchId, severity, page, refreshKey]);
  if (!data) return <div className="h-24 animate-pulse rounded-xl bg-slate-100" />;
  const tabs = [["error", "Errors", data.counts.errors, "text-rose-700"], ["warning", "Warnings", data.counts.warnings, "text-amber-700"],
    ["pass", "Passed", data.counts.passed, "text-emerald-700"]] as const;
  const pages = Math.max(1, Math.ceil(data.total / 20));
  return (
    <div>
      <div className="flex gap-1 border-b border-slate-200">
        {tabs.map(([key, label, n, tone]) => (
          <button key={key} type="button" onClick={() => setSeverity(key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${severity === key ? "border-indigo-600 text-slate-900" : "border-transparent text-slate-500"}`}>
            {label} <span className={tone}>{n}</span>
          </button>
        ))}
      </div>
      {data.items.length === 0 ? <p className="py-6 text-center text-sm text-slate-400">Nothing here.</p> : (
        <ul className="divide-y divide-slate-100 text-xs">
          {data.items.map((v, i) => (
            <li key={i} className="py-2">
              <span className="font-mono text-[10px] text-slate-400">{v.code}</span>{" "}
              {v.employee_name && <span className="font-medium text-slate-800">{v.employee_name} </span>}
              <span className="text-slate-600">{v.message}</span>
            </li>
          ))}
        </ul>
      )}
      {pages > 1 && (
        <div className="flex items-center justify-between pt-2 text-xs text-slate-600">
          <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Prev</button>
          <span>Page {page} of {pages}</span>
          <button type="button" disabled={page >= pages} onClick={() => setPage(page + 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next ›</button>
        </div>
      )}
    </div>
  );
}

type Dialog = "start" | "validate" | "approve" | "finalize" | "rollback" | "recalculate" | "attendance" | "reopen" | null;

export function PayrollRunsPage({ token, onExpired }: { token: string; onExpired: () => void }) {
  const toast = useToast();
  const can = useCan();
  const [months, setMonths] = useState<PayrollMonth[] | null>(null);
  const [period, setPeriod] = useState<string | null>(null);
  const [detail, setDetail] = useState<MonthDetail | null>(null);
  const [checks, setChecks] = useState<Prechecks | null>(null);
  const [checking, setChecking] = useState(false);
  const [departments, setDepartments] = useState<string[]>([]);
  const [department, setDepartment] = useState("");
  const [workState, setWorkState] = useState("");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [challan, setChallan] = useState<Challan | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onExpired();
    setError(e instanceof ApiError ? e.message : "Could not load payroll runs");
  }, [onExpired]);

  const loadMonths = useCallback(() => payrollMonths(token).then((m) => {
    setMonths(m);
    setPeriod((cur) => cur ?? m.find((x) => x.status !== "NOT_STARTED")?.period ?? m[0]?.period ?? null);
  }).catch(fail), [token, fail]);

  const loadDetail = useCallback(() => {
    if (!period) return Promise.resolve();
    return payrollMonth(token, period).then(setDetail).catch(fail);
  }, [token, period, fail]);

  const runChecks = useCallback(() => {
    if (!period) return;
    setChecking(true);
    payrollPrechecks(token, period, department, workState).then(setChecks)
      .catch((e) => { setChecks(null); if (e instanceof ApiError) toast(e.message, "error"); else fail(e); })
      .finally(() => setChecking(false));
  }, [token, period, department, workState, fail, toast]);

  useEffect(() => { void loadMonths(); }, [loadMonths]);
  useEffect(() => { directoryFilters(token).then((f) => setDepartments(f.departments)).catch(() => undefined); }, [token]);
  useEffect(() => { setDetail(null); setChallan(null); void loadDetail(); }, [loadDetail]);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- re-check when the month changes, scope via the button
  useEffect(() => { setChecks(null); runChecks(); }, [period]);

  const latest = detail?.batches[0];
  // poll while a batch is being reviewed or processed (the LOP card drives the run itself)
  useEffect(() => {
    if (!latest || latest.finished) return;
    const t = setInterval(() => { void loadDetail(); }, 3000);
    return () => clearInterval(t);
  }, [latest, loadDetail]);

  useEffect(() => {
    if (period && latest?.stage === "FINALIZED") pfChallan(token, period).then(setChallan).catch(() => undefined);
  }, [token, period, latest?.stage]);

  async function after(message: string) {
    toast(message, "success");
    setRefreshKey((k) => k + 1);
    await Promise.all([loadDetail(), loadMonths()]);
  }

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  const scopeText = [department, workState.trim()].filter(Boolean).join(" · ") || "All employees";
  // a new batch only before validation (it processes employees not yet in this month)
  const canStart = !latest || latest.status === "CANCELLED"
    || (latest.finished && (!latest.stage || latest.stage === "VALIDATION_FAILED"));
  const finalized = !!detail && (detail.locked > 0 || latest?.stage === "FINALIZED");
  const attendanceMissing = !!checks?.checks.find((c) => c.key === "ATTENDANCE" && c.failed > 0);
  const step = stepIndex(latest);
  // re-run processed, unlocked payslips (e.g. after a data or rule fix); not while a batch runs
  const canRecalculate = !!detail && detail.employees > detail.locked
    && (!latest || latest.finished || latest.status === "CANCELLED") && latest?.stage !== "FINALIZED";

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <Card className="overflow-x-auto p-0">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500">
            <tr><th className="px-5 py-2.5 font-medium">Payroll month</th><th className="px-3 py-2.5 text-right font-medium">Employees</th>
              <th className="px-3 py-2.5 text-right font-medium">Gross</th><th className="px-3 py-2.5 text-right font-medium">Deductions</th>
              <th className="px-3 py-2.5 text-right font-medium">Net pay</th><th className="px-5 py-2.5 font-medium">Status</th></tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {!months && <tr><td colSpan={6} className="p-5"><div className="h-16 animate-pulse rounded-lg bg-slate-100" /></td></tr>}
            {months?.map((m) => (
              <tr key={m.period} onClick={() => setPeriod(m.period)}
                className={`cursor-pointer ${period === m.period ? "bg-indigo-50/60" : "hover:bg-slate-50"}`}>
                <td className="px-5 py-3 font-medium text-slate-900">{m.label}</td>
                <td className="px-3 py-3 text-right tabular-nums">{m.employees}</td>
                <td className="px-3 py-3 text-right tabular-nums">{formatINR(m.gross)}</td>
                <td className="px-3 py-3 text-right tabular-nums">{formatINR(m.deductions)}</td>
                <td className="px-3 py-3 text-right font-medium tabular-nums">{formatINR(m.net)}</td>
                <td className="px-5 py-3"><Badge status={m.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {period && (
        <>
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">{detail?.label ?? period}</h2>
                <p className="text-xs text-slate-500">
                  {detail ? `${detail.employees} payslips · ${detail.locked} locked · net ${formatINR(detail.net)}` : "Loading…"}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {canRecalculate && can("run_payroll") && (
                  <button type="button" onClick={() => setDialog("recalculate")}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">
                    <RefreshCw className="h-3.5 w-3.5" />Recalculate month
                  </button>
                )}
                {finalized && can("approve") && (
                  <button type="button" onClick={() => setDialog("reopen")}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-50">
                    <RotateCcw className="h-3.5 w-3.5" />Reopen month
                  </button>
                )}
                {detail && <Badge status={detail.status} />}
              </div>
            </div>
            <div className="mt-4"><Stepper current={step} /></div>
          </Card>

          {latest && ACTIVE.includes(latest.status) && (
            <BulkLopCard key={latest.batch_id} batchId={latest.batch_id} token={token} />
          )}

          {latest && latest.finished && latest.status !== "CANCELLED" && (
            <Card>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><ClipboardCheck className="h-4 w-4 text-slate-400" />Latest run</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {latest.done} calculated · {latest.skipped} skipped · {latest.failed} failed
                    {latest.created_by && ` · started by ${latest.created_by}`}
                    {latest.approved_by && ` · approved by ${latest.approved_by}`}
                    {latest.finalized_by && ` · finalized by ${latest.finalized_by}`}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {latest.stage !== "FINALIZED" && latest.stage !== "APPROVED" && can("run_payroll") && (
                    <button type="button" onClick={() => setDialog("validate")}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700">
                      <ShieldCheck className="h-4 w-4" />{latest.stage ? "Validate again" : "Validate"}
                    </button>
                  )}
                  {latest.stage === "VALIDATED" && can("approve") && (
                    <button type="button" onClick={() => setDialog("approve")}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-700">
                      <BadgeCheck className="h-4 w-4" />Approve
                    </button>
                  )}
                  {latest.stage === "APPROVED" && can("approve") && (
                    <button type="button" onClick={() => setDialog("finalize")}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700">
                      <Lock className="h-4 w-4" />Finalize
                    </button>
                  )}
                  {latest.stage !== "FINALIZED" && can("run_payroll") && (
                    <button type="button" onClick={() => setDialog("rollback")}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
                      <RotateCcw className="h-4 w-4" />Roll back
                    </button>
                  )}
                </div>
              </div>
              {latest.stage && latest.stage !== "APPROVED" && latest.stage !== "FINALIZED" && (
                <div className="mt-4"><ValidationList token={token} batchId={latest.batch_id} refreshKey={refreshKey} /></div>
              )}
              {latest.stage === "FINALIZED" && challan && (
                <div className="mt-4 rounded-xl bg-slate-50 p-4">
                  <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Landmark className="h-4 w-4 text-slate-400" />EPF challan · {challan.contributing_members} members</p>
                  <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                    {challan.accounts.map((a) => (
                      <div key={a.account} className="flex justify-between"><dt className="text-slate-500">{a.account} · {a.name}</dt><dd className="tabular-nums">{formatINR(a.amount)}</dd></div>
                    ))}
                    <div className="flex justify-between font-semibold"><dt>Total</dt><dd className="tabular-nums">{formatINR(challan.total)}</dd></div>
                  </dl>
                </div>
              )}
              {latest.failed > 0 && <div className="mt-4"><BulkLopCard key={`f${latest.batch_id}`} batchId={latest.batch_id} token={token} /></div>}
            </Card>
          )}

          {canStart && (
            <Card>
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><ScanSearch className="h-4 w-4 text-slate-400" />Pre-checks</p>
                  <p className="text-xs text-slate-500">Run before payroll. Read-only: nothing is changed.</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select value={department} onChange={(e) => setDepartment(e.target.value)} aria-label="Department"
                    className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs">
                    <option value="">All departments</option>
                    {departments.map((d) => <option key={d}>{d}</option>)}
                  </select>
                  <input value={workState} onChange={(e) => setWorkState(e.target.value)} placeholder="State / office (optional)" maxLength={50}
                    className="w-44 rounded-lg border border-slate-300 px-2 py-1.5 text-xs" />
                  <button type="button" onClick={runChecks} disabled={checking}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                    <RefreshCw className={`h-3.5 w-3.5 ${checking ? "animate-spin" : ""}`} />Run checks
                  </button>
                </div>
              </div>
              <div className="mt-4">
                {!checks ? <div className="h-28 animate-pulse rounded-xl bg-slate-100" /> : (
                  <>
                    <p className="mb-2 text-xs text-slate-500">
                      {checks.employees} employees in scope{checks.already_processed ? ` · ${checks.already_processed} already processed (will be skipped)` : ""}
                      {" · "}<span className="text-emerald-700">{checks.summary.passed} passed</span>
                      {" · "}<span className="text-amber-700">{checks.summary.warnings} with warnings</span>
                      {" · "}<span className="text-rose-700">{checks.summary.failed} failed</span>
                    </p>
                    <PrecheckGrid data={checks} />
                    {checks.blocking.length > 0 && <p className="mt-2 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700">Blocked: {checks.blocking.join("; ")}</p>}
                    {attendanceMissing && can("edit_attendance") && (
                      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
                        <span>Some employees have no attendance for {checks.label}; their payroll will fail.</span>
                        <button type="button" onClick={() => setDialog("attendance")}
                          className="rounded-lg bg-amber-600 px-2.5 py-1 font-medium text-white hover:bg-amber-700">Create attendance for all employees</button>
                      </div>
                    )}
                    <div className="mt-4 flex justify-end">
                      <button type="button" onClick={() => setDialog("start")} disabled={!checks.can_run || !can("run_payroll")}
                        title={can("run_payroll") ? undefined : "Your role cannot run payroll"}
                        className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50">
                        <CirclePlay className="h-4 w-4" />Start payroll · {checks.label}
                      </button>
                    </div>
                  </>
                )}
              </div>
            </Card>
          )}

          <PayrollRegister token={token} month={period} label={detail?.label ?? period} refreshKey={refreshKey}
            canRun={(!latest || latest.finished || latest.status === "CANCELLED") && can("run_payroll")}
            onChanged={() => { setRefreshKey((k) => k + 1); void loadDetail(); runChecks(); }} />

          {detail && detail.batches.length > 1 && (
            <Card>
              <p className="text-sm font-semibold text-slate-900">Batch history · {detail.batches.length}</p>
              <ul className="mt-2 divide-y divide-slate-100 text-xs">
                {detail.batches.map((b) => (
                  <li key={b.batch_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span className="text-slate-600">{b.created_at ? new Date(b.created_at).toLocaleString("en-IN") : ""} · {b.created_by ?? "—"} · {Array.isArray((b.filters as Record<string, unknown>).employees)
                      ? `Selected employees (${((b.filters as Record<string, unknown>).employees as string[]).length})`
                      : <>{(b.filters as Record<string, unknown>).recalculate ? "Recalculation · " : ""}{[b.filters.department, b.filters.work_state].filter(Boolean).join(" · ") || "All employees"}</>}</span>
                    <span className="flex items-center gap-2 text-slate-500">{b.done} done · {b.skipped} skipped · {b.failed} failed <Badge status={b.stage ?? b.status} /></span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}

      {dialog === "start" && checks && (
        <ConfirmDialog title={`Start payroll for ${checks.label}`} confirmLabel="Start LOP review"
          facts={[
            { label: "Month", value: checks.label }, { label: "Scope", value: scopeText },
            { label: "Employees", value: checks.employees },
            { label: "Already processed (skipped)", value: checks.already_processed },
            { label: "Checks", value: `${checks.summary.passed} ok · ${checks.summary.warnings} warning · ${checks.summary.failed} failed` },
          ]}
          warning={checks.summary.failed ? "Employees with failed checks will fail in the run. Fix their data and use 'Retry failed'." : undefined}
          onClose={() => setDialog(null)}
          onConfirm={async () => { await startBatch(token, period!, department, workState); await after("LOP review started"); }}>
          <p className="text-xs text-slate-500">Next you review LOP for absences without approved leave. Payroll is calculated only when you press Run in that card.</p>
        </ConfirmDialog>
      )}
      {dialog === "validate" && latest && (
        <ConfirmDialog title="Validate payroll" confirmLabel="Run validation"
          facts={[{ label: "Month", value: detail!.label }, { label: "Calculated", value: latest.done }, { label: "Failed", value: latest.failed }]}
          onClose={() => setDialog(null)}
          onConfirm={async () => { const r = await validateBatch(token, latest.batch_id); await after(`Validation: ${r.errors} errors, ${r.warnings} warnings`); }} />
      )}
      {dialog === "approve" && latest && (
        <ConfirmDialog title="Approve payroll" confirmLabel="Approve" tone="indigo"
          facts={[{ label: "Month", value: detail!.label }, { label: "Payslips", value: latest.done }, { label: "Net pay", value: formatINR(detail!.net) },
            { label: "Prepared by", value: latest.created_by ?? "—" }]}
          warning="Maker-checker: the HR user who started this payroll cannot approve it."
          onClose={() => setDialog(null)}
          onConfirm={async () => { await approveBatch(token, latest.batch_id); await after("Payroll approved"); }} />
      )}
      {dialog === "finalize" && latest && (
        <ConfirmDialog title="Finalize payroll" confirmLabel="Finalize and lock" tone="emerald"
          facts={[{ label: "Month", value: detail!.label }, { label: "Payslips to lock", value: latest.done }, { label: "Net pay", value: formatINR(detail!.net) }]}
          warning="Finalizing locks these payslips. A locked month cannot be recalculated or rolled back."
          onClose={() => setDialog(null)}
          onConfirm={async () => { await finalizeBatch(token, latest.batch_id); await after("Payroll finalized and locked"); }} />
      )}
      {dialog === "recalculate" && detail && (
        <ConfirmDialog title={`Recalculate ${detail.label}`} confirmLabel="Recalculate" reasonLabel="Reason (required)"
          facts={[{ label: "Month", value: detail.label }, { label: "Payslips to recalculate", value: detail.employees - detail.locked },
            { label: "Locked (not touched)", value: detail.locked }]}
          warning="Every processed, unlocked payslip of the month is calculated again with today's data and rules (version +1). Validation and approval of the month must be done again."
          onClose={() => setDialog(null)}
          onConfirm={async (reason) => { await recalculateMonth(token, period!, reason); await after("Recalculation queued"); }} />
      )}
      {dialog === "attendance" && checks && (
        <ConfirmDialog title={`Create attendance · ${checks.label}`} confirmLabel="Create attendance" reasonLabel="Reason (required)"
          facts={[{ label: "Month", value: checks.label }, { label: "Employees in scope", value: checks.employees },
            { label: "Days created", value: "Missing days up to today" }, { label: "Pattern", value: "Sunday week off, other days present" }]}
          warning="Existing attendance is never changed. Afterwards correct absences, half days and holidays per employee (Attendance button in the list), then run or retry payroll."
          onClose={() => setDialog(null)}
          onConfirm={async (reason) => {
            const r = await createMonthAttendance(token, period!, reason);
            await after(`Attendance created: ${r.days} days for ${r.employees} employees (up to ${r.up_to})`);
            runChecks();
          }} />
      )}
      {dialog === "reopen" && detail && (
        <ConfirmDialog title={`Reopen ${detail.label}`} confirmLabel="Reopen month" tone="rose" reasonLabel="Reason (required, at least 10 characters)"
          facts={[{ label: "Month", value: detail.label }, { label: "Locked payslips", value: detail.locked }]}
          warning="Payslips become editable again. The month must be validated, approved and finalized again. Do this only if statutory returns for the month are not filed yet."
          onClose={() => setDialog(null)}
          onConfirm={async (reason) => { const r = await reopenMonth(token, period!, reason); await after(`Month reopened: ${r.unlocked} payslips unlocked`); }} />
      )}
      {dialog === "rollback" && latest && (
        <ConfirmDialog title="Roll back payroll" confirmLabel="Roll back" tone="rose" reasonLabel="Reason (required)"
          facts={[{ label: "Month", value: detail!.label }, { label: "Payslips deleted", value: latest.done }, { label: "LOP decisions", value: "kept" }]}
          warning="The calculated payslips of this batch are removed and the batch goes back to LOP review. The audit trail keeps everything."
          onClose={() => setDialog(null)}
          onConfirm={async (reason) => { await rollbackBatch(token, latest.batch_id, reason); await after("Payroll rolled back to LOP review"); }} />
      )}
    </div>
  );
}
