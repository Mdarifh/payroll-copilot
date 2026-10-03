import { CirclePlay, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, runSelected, type BatchStatus, type RegisterRow } from "../lib/api";
import { formatDays } from "../lib/format";

// Review & confirm a payroll run for selected employees, with LOP editable per employee.
// Changed LOP is saved (validated + audited by the backend) just before the run starts.

const STATUS_TEXT = { PROCESSED: "recalculated", LOCKED: "locked", FAILED: "failed before", NOT_PROCESSED: "new payslip" };

export function RunSelectedDialog({ token, month, label, rows, onClose, onDone }: {
  token: string; month: string; label: string; rows: RegisterRow[];
  onClose: () => void; onDone: (batch: BatchStatus) => void;
}) {
  const [lop, setLop] = useState<Record<string, string>>(() => Object.fromEntries(rows.map((r) => [r.employee_code, String(Number(r.lop_days))])));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const changed = rows.filter((r) => lop[r.employee_code] !== "" && Number(lop[r.employee_code]) !== Number(r.lop_days));
  const invalid = rows.filter((r) => {
    const v = lop[r.employee_code];
    const n = Number(v);
    return v === "" || !Number.isFinite(n) || n < 0 || n > 31 || (n * 2) % 1 !== 0;
  });
  const noAttendance = rows.filter((r) => !r.attendance_days);
  const undecided = rows.filter((r) => Number(r.undecided_absences) > 0);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const lopDays = Object.fromEntries(changed.map((r) => [r.employee_code, lop[r.employee_code]]));
      onDone(await runSelected(token, month, rows.map((r) => r.employee_code), reason.trim(), lopDays));
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not start the run. Nothing was changed.");
    } finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => !busy && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={`Run payroll ${label}`} onClick={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
          <div>
            <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">Review &amp; confirm</p>
            <h2 className="text-base font-semibold text-slate-900">Run payroll · {label} · {rows.length} employee{rows.length === 1 ? "" : "s"}</h2>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className="rounded-lg p-1 text-slate-400 hover:bg-slate-100"><X className="h-4 w-4" /></button>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4 text-sm">
          <p className="text-xs text-slate-500">Edit LOP days where needed (steps of 0.5). Only these employees are calculated; nobody else's payslip changes.</p>
          <div className="overflow-x-auto rounded-xl border border-slate-200">
            <table className="w-full min-w-[600px] text-sm">
              <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500">
                <tr><th className="px-3 py-2">Employee</th><th className="px-3 py-2">Payslip</th><th className="px-3 py-2 text-right">Attendance</th>
                  <th className="px-3 py-2 text-right">Absent, no LOP decided</th><th className="px-3 py-2 text-right">LOP days</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.map((r) => {
                  const value = lop[r.employee_code] ?? "";
                  const isChanged = changed.includes(r);
                  const absences = Number(r.undecided_absences);
                  return (
                    <tr key={r.employee_code} className={isChanged ? "bg-amber-50/60" : ""}>
                      <td className="px-3 py-2"><span className="font-medium text-slate-900">{r.full_name}</span><span className="block text-xs text-slate-400">{r.employee_code}</span></td>
                      <td className="px-3 py-2 text-xs text-slate-600">{STATUS_TEXT[r.status]}</td>
                      <td className="px-3 py-2 text-right text-xs">{r.attendance_days ? `${r.attendance_days} days` : <span className="text-rose-600">none</span>}</td>
                      <td className="px-3 py-2 text-right text-xs">
                        {absences > 0 ? (
                          <button type="button" onClick={() => setLop({ ...lop, [r.employee_code]: String(absences) })}
                            title="Use the absences as LOP" className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 hover:bg-amber-200">
                            {formatDays(r.undecided_absences)} → use as LOP
                          </button>
                        ) : <span className="text-slate-400">—</span>}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <input type="number" min={0} max={31} step={0.5} value={value} disabled={!r.attendance_days}
                          title={r.attendance_days ? undefined : "No attendance for this month - create attendance first"}
                          onChange={(e) => setLop({ ...lop, [r.employee_code]: e.target.value })}
                          className={`w-20 rounded-lg border px-2 py-1 text-right text-sm outline-none focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-100 ${invalid.includes(r) ? "border-rose-400" : isChanged ? "border-amber-400" : "border-slate-300"}`} />
                        {isChanged && <span className="block text-[10px] text-amber-700">was {formatDays(r.lop_days)}</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {noAttendance.length > 0 && (
            <p className="flex gap-1.5 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />
              {noAttendance.length} employee(s) have no attendance this month and will fail: {noAttendance.map((r) => r.full_name).join(", ")}.</p>
          )}
          {undecided.length > 0 && (
            <p className="text-xs text-amber-800">{undecided.length} employee(s) have absences without approved leave and no LOP decided — those days are paid unless you set LOP.</p>
          )}
          <label className="block text-xs font-medium text-slate-600">Reason (required, saved with the run and any LOP change)
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={500}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
          </label>
          {error && <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-slate-200 bg-slate-50 px-5 py-3">
          <span className="text-xs text-slate-500">{changed.length ? `${changed.length} LOP change(s) will be saved first` : "LOP unchanged"}</span>
          <div className="flex gap-2">
            <button type="button" onClick={onClose} disabled={busy} className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-200">Cancel</button>
            <button type="button" onClick={run} disabled={busy || reason.trim().length < 5 || invalid.length > 0}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
              <CirclePlay className="h-4 w-4" />{busy ? "Starting…" : `Run payroll for ${rows.length}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
