import { ArrowLeft, ShieldCheck, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, applyChange, simulateChange, type ActionItem, type ChangePreview, type PayrollChange } from "../lib/api";
import { formatDays } from "../lib/format";
import { DiffTable } from "./InsightCards";
import { useToast } from "./layout/Toast";

// Review & confirm for every payroll change: form -> preview (backend simulation, nothing
// saved) -> Confirm & Apply. The apply carries the preview id, so it can only save exactly
// what was shown; the backend refuses if the data changed in between.

export type ChangeKind = "lop" | "overtime" | "adjustment" | "salary" | "attendance";

const TITLES: Record<ChangeKind, string> = {
  lop: "Edit LOP", overtime: "Add overtime", adjustment: "Add one-off pay", salary: "Revise salary",
  attendance: "Edit attendance",
};
const STATUSES = [["PRESENT", "Present"], ["ABSENT", "Absent"], ["HALF_DAY", "Half day"],
  ["WEEK_OFF", "Week off"], ["HOLIDAY", "Holiday"]] as const;
const HEADS = {
  EARNING: [["OTHER_EARNING", "Other earning (taxable)"], ["OTHER_EARNING_NT", "Other earning (non-taxable)"], ["BONUS", "Bonus"]],
  DEDUCTION: [["OTHER_DED", "Other deduction"], ["LOAN", "Loan recovery"]],
} as const;

export function kindOf(change: Partial<PayrollChange>): ChangeKind {
  if (change.salary) return "salary";
  if (change.attendance?.length || change.fill_month) return "attendance";
  if (change.overtime?.length) return "overtime";
  if (change.adjustments?.length) return "adjustment";
  return "lop";
}

const field = "mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100";

export function ChangeDialog({ token, kind, employeeCode, employeeName, month, initial, onClose, onApplied, source = "ui", currentLop }: {
  token: string; kind: ChangeKind; employeeCode: string; employeeName: string; month: string;
  initial?: Partial<PayrollChange>; onClose: () => void; onApplied?: (result: ActionItem) => void;
  source?: "ui" | "chat";
  /** today's LOP days of the month (attendance dialog: "also apply LOP") */
  currentLop?: string;
}) {
  const toast = useToast();
  const monthEnd = new Date(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0);
  const today = new Date();
  const lastDay = (monthEnd < today ? monthEnd : today).toISOString().slice(0, 10);
  const [days, setDays] = useState(initial?.lop_days ?? "");
  const [workDate, setWorkDate] = useState(initial?.overtime?.[0]?.work_date ?? lastDay);
  const [hours, setHours] = useState(initial?.overtime?.[0]?.hours ?? "");
  const [adjKind, setAdjKind] = useState<"EARNING" | "DEDUCTION">(initial?.adjustments?.[0]?.kind ?? "EARNING");
  const [head, setHead] = useState(initial?.adjustments?.[0]?.head_code ?? "OTHER_EARNING");
  const [amount, setAmount] = useState(initial?.adjustments?.[0]?.amount ?? "");
  const [label, setLabel] = useState(initial?.adjustments?.[0]?.label ?? "");
  const [gross, setGross] = useState(initial?.salary?.monthly_gross ?? "");
  const [effective, setEffective] = useState(initial?.salary?.effective_from ?? `${month}-01`);
  const [reason, setReason] = useState(initial?.reason && initial.reason !== "Requested in chat" ? initial.reason : "");
  const fillMonth = !!initial?.fill_month;
  const [attDate, setAttDate] = useState(initial?.attendance?.[0]?.date ?? lastDay);
  const [attStatus, setAttStatus] = useState(initial?.attendance?.[0]?.status ?? "PRESENT");
  const [attRemarks, setAttRemarks] = useState(initial?.attendance?.[0]?.remarks ?? "");
  // an absence normally means loss of pay: on by default, HR can untick it (e.g. a late
  // leave application is expected)
  const [withLop, setWithLop] = useState(kind === "attendance");
  const [preview, setPreview] = useState<ChangePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  function change(): PayrollChange {
    const base = { reason: reason.trim() };
    switch (kind) {
      case "lop": return { ...base, lop_days: String(days) };
      case "overtime": return { ...base, overtime: [{ work_date: workDate, hours: String(hours) }] };
      case "adjustment": return { ...base, adjustments: [{ kind: adjKind, head_code: head, amount: String(amount).replace(/[,₹\s]/g, ""), label: label.trim() || null }] };
      case "salary": return { ...base, salary: { monthly_gross: String(gross).replace(/[,₹\s]/g, ""), effective_from: effective } };
      case "attendance": {
        if (fillMonth) return { ...base, fill_month: true };
        const out: PayrollChange = { ...base, attendance: [{ date: attDate, status: attStatus, remarks: attRemarks.trim() || null }] };
        const lopDay = attStatus === "ABSENT" ? 1 : attStatus === "HALF_DAY" ? 0.5 : 0;
        if (withLop && lopDay) out.lop_days = String(Number(currentLop ?? initial?.lop_days ?? 0) + lopDay);
        return out;
      }
    }
  }
  const salaryMonth = kind === "salary" ? effective.slice(0, 7) : month;
  const ready = reason.trim().length >= 5 && (kind === "lop" ? days !== "" : kind === "overtime" ? hours !== "" && workDate
    : kind === "adjustment" ? amount !== "" : kind === "attendance" ? (fillMonth || !!attDate) : gross !== "" && effective);

  async function run(step: "preview" | "apply") {
    setBusy(true);
    setError(null);
    try {
      if (step === "preview") {
        setPreview(await simulateChange(token, employeeCode, salaryMonth, change()));
      } else if (preview) {
        const result = await applyChange(token, employeeCode, salaryMonth, change(), preview.preview_id, source);
        if (result.status === "PENDING_APPROVAL") toast("Sent for approval - a different HR user approves it in the Action Center (you can't approve your own request)", "info");
        else if (result.status === "APPLIED") toast(`Applied for ${employeeName}${result.result?.recalculated?.length ? ` · payslip recalculated (${result.result.recalculated.join(", ")})` : ""}`, "success");
        else toast(result.message ?? `Could not apply: ${result.error}`, "error");
        onApplied?.(result);
        onClose();
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Nothing was changed.");
      if (e instanceof ApiError && /preview/i.test(e.message)) setPreview(null); // stale: preview again
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => !busy && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={TITLES[kind]} onClick={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-50 text-indigo-600"><ShieldCheck className="h-5 w-5" /></span>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{preview ? "Review & confirm" : "Step 1 of 2 · details"}</p>
              <h2 className="text-base font-semibold text-slate-900">{TITLES[kind]} · {employeeName}</h2>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className="rounded-lg p-1 text-slate-400 hover:bg-slate-100"><X className="h-4 w-4" /></button>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4 text-sm">
          {!preview ? (
            <>
              <p className="text-xs text-slate-500">{kind === "salary" ? "New structure from the effective date (same template)" : `Payroll month ${month}`}</p>
              {kind === "lop" && (
                <label className="block text-xs font-medium text-slate-600">New LOP days for the month
                  <input type="number" min={0} step={0.5} value={days} onChange={(e) => setDays(e.target.value)} className={field} />
                </label>
              )}
              {kind === "overtime" && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs font-medium text-slate-600">Work date
                    <input type="date" value={workDate} max={lastDay} onChange={(e) => setWorkDate(e.target.value)} className={field} />
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Hours
                    <input type="number" min={0.5} step={0.5} value={hours} onChange={(e) => setHours(e.target.value)} className={field} />
                  </label>
                  <p className="col-span-2 text-[11px] text-slate-400">The rate comes from the employee's agreed rate or the company overtime policy — never typed in.</p>
                </div>
              )}
              {kind === "adjustment" && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs font-medium text-slate-600">Type
                    <select value={adjKind} onChange={(e) => { const k = e.target.value as "EARNING" | "DEDUCTION"; setAdjKind(k); setHead(HEADS[k][0][0]); }} className={field}>
                      <option value="EARNING">Earning</option><option value="DEDUCTION">Deduction</option>
                    </select>
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Component
                    <select value={head} onChange={(e) => setHead(e.target.value)} className={field}>
                      {HEADS[adjKind].map(([code, name]) => <option key={code} value={code}>{name}</option>)}
                    </select>
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Amount (₹)
                    <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" className={field} />
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Label (optional)
                    <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100} placeholder="e.g. Referral bonus" className={field} />
                  </label>
                </div>
              )}
              {kind === "attendance" && fillMonth && (
                <p className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
                  Creates the missing attendance days of {month} up to today: Sundays as week off, other employed days as present.
                  Afterwards click any day on the calendar to correct it (absent, half day, holiday…).
                </p>
              )}
              {kind === "attendance" && !fillMonth && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs font-medium text-slate-600">Date
                    <input type="date" value={attDate} min={`${month}-01`} max={lastDay} onChange={(e) => setAttDate(e.target.value)} className={field} />
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Status
                    <select value={attStatus} onChange={(e) => setAttStatus(e.target.value)} className={field}>
                      {STATUSES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </select>
                  </label>
                  <label className="col-span-2 block text-xs font-medium text-slate-600">Remarks (optional)
                    <input value={attRemarks} onChange={(e) => setAttRemarks(e.target.value)} maxLength={200} className={field} />
                  </label>
                  {(attStatus === "ABSENT" || attStatus === "HALF_DAY") && (
                    <label className="col-span-2 flex items-center gap-2 text-xs text-slate-700">
                      <input type="checkbox" checked={withLop} onChange={(e) => setWithLop(e.target.checked)} />
                      Also apply LOP for this day (LOP {formatDays(currentLop ?? "0")} → {formatDays(String(Number(currentLop ?? 0) + (attStatus === "ABSENT" ? 1 : 0.5)))} days)
                    </label>
                  )}
                  {(attStatus === "ABSENT" || attStatus === "HALF_DAY") && !withLop && (
                    <p className="col-span-2 rounded-md bg-amber-50 px-2 py-1 text-[11px] text-amber-800">
                      Net pay will not change: the day is marked {attStatus === "ABSENT" ? "absent" : "half day"} but LOP stays {formatDays(currentLop ?? "0")} day(s).
                    </p>
                  )}
                  <p className="col-span-2 text-[11px] text-slate-400">Leave days come from approved leave applications. Marking a day absent does not cut pay unless LOP is applied.</p>
                </div>
              )}
              {kind === "salary" && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs font-medium text-slate-600">New monthly gross (₹)
                    <input value={gross} onChange={(e) => setGross(e.target.value)} inputMode="decimal" className={field} />
                  </label>
                  <label className="block text-xs font-medium text-slate-600">Effective from
                    <input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} className={field} />
                  </label>
                </div>
              )}
              <label className="block text-xs font-medium text-slate-600">Reason (required, saved in the audit log)
                <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={500} className={field} />
              </label>
            </>
          ) : (
            <>
              <p className="rounded-lg bg-indigo-50 px-3 py-2 text-xs text-indigo-800">
                Preview from the payroll engine for {preview.period_label}. Nothing is saved until you confirm.
              </p>
              <DiffTable rows={preview.rows} />
              {preview.overtime.map((o) => <p key={o.work_date} className="text-xs text-slate-600">{o.formula} <span className="text-slate-400">({o.rate_source})</span></p>)}
              {preview.warnings.length > 0 && (
                <div className="space-y-1 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  {preview.warnings.map((w) => <p key={w} className="flex gap-1.5"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />{w}</p>)}
                </div>
              )}
              <p className="text-xs text-slate-500">Reason: {reason}</p>
              {preview.requires_approval && (
                <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  Change approval is ON: this goes to the Action Center and is applied when a different HR user approves it.
                  (HR can switch approval off in the Action Center.)
                </p>
              )}
              <p className="text-[11px] text-slate-400">If changes need approval, this goes to the Action Center and is applied when another HR user approves it. When applied, a processed payslip is recalculated and a validated or approved month must be validated again.</p>
            </>
          )}
          {error && <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-slate-200 bg-slate-50 px-5 py-3">
          {preview ? (
            <button type="button" onClick={() => setPreview(null)} disabled={busy}
              className="inline-flex items-center gap-1 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-200"><ArrowLeft className="h-4 w-4" />Edit</button>
          ) : <span />}
          <div className="flex gap-2">
            <button type="button" onClick={onClose} disabled={busy} className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-200">Cancel</button>
            {preview ? (
              <button type="button" onClick={() => run("apply")} disabled={busy}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">{busy ? "Submitting…" : preview.requires_approval ? "Submit for approval" : "Confirm & Apply"}</button>
            ) : (
              <button type="button" onClick={() => run("preview")} disabled={busy || !ready}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">{busy ? "Calculating…" : "Review impact"}</button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
