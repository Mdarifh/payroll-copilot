import { CalendarDays, CalendarPlus, Check, ClipboardCheck, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { LeaveApplyForm } from "../components/LeaveTaxCards";
import { useToast } from "../components/layout/Toast";
import { ApiError, cancelLeave, decideLeave, leavesToApprove, myLeaves, type LeaveRow } from "../lib/api";

// My leave (balance, requests, apply) and - for managers / HR - requests to decide.
// The backend checks every application and writes approved leave into attendance.

const TONE: Record<LeaveRow["status"], string> = {
  PENDING: "bg-amber-50 text-amber-700", APPROVED: "bg-emerald-50 text-emerald-700",
  REJECTED: "bg-rose-50 text-rose-700", CANCELLED: "bg-slate-100 text-slate-500",
};
const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
const span = (l: LeaveRow) => (l.from === l.to ? day(l.from) : `${day(l.from)} – ${day(l.to)}`);

export function LeavePage({ token, onExpired }: { token: string; onExpired: () => void }) {
  const toast = useToast();
  const [data, setData] = useState<Awaited<ReturnType<typeof myLeaves>> | null>(null);
  const [queue, setQueue] = useState<LeaveRow[]>([]);
  const [formKey, setFormKey] = useState(0);
  const [deciding, setDeciding] = useState<{ leave: LeaveRow; approve: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    myLeaves(token).then(setData).catch((e) => {
      if (e instanceof ApiError && e.status === 401) return onExpired();
      setError(e instanceof ApiError ? e.message : "Could not load leave");
    });
    leavesToApprove(token).then(setQueue).catch(() => undefined);
  }, [token, onExpired]);
  useEffect(load, [load]);

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  return (
    <div className="space-y-4 p-4 sm:p-6">
      {queue.length > 0 && (
        <section className="rounded-2xl border border-amber-200 bg-white p-5 shadow-sm">
          <p className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-900"><ClipboardCheck className="h-4 w-4 text-amber-500" />To approve ({queue.length})</p>
          <ul className="divide-y divide-slate-100">
            {queue.map((l) => (
              <li key={l.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
                <div className="text-sm">
                  <p className="font-medium text-slate-900">{l.employee_name} <span className="font-normal text-slate-400">{l.employee_code}</span></p>
                  <p className="text-xs text-slate-600">{l.leave_label} · {span(l)} · {l.days} day(s){l.reason ? ` · “${l.reason}”` : ""}</p>
                </div>
                <div className="flex gap-2">
                  <button type="button" onClick={() => setDeciding({ leave: l, approve: true })} className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700"><Check className="h-3.5 w-3.5" />Approve</button>
                  <button type="button" onClick={() => setDeciding({ leave: l, approve: false })} className="inline-flex items-center gap-1 rounded-lg border border-rose-200 px-2.5 py-1 text-xs font-medium text-rose-700 hover:bg-rose-50"><X className="h-3.5 w-3.5" />Reject</button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        {!data ? [0, 1, 2].map((i) => <div key={i} className="h-20 animate-pulse rounded-2xl bg-slate-100" />) : data.balances.map((b) => (
          <div key={b.leave_type} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <p className="text-xs font-medium text-slate-500">{b.label} · FY {data.financial_year}</p>
            <p className="mt-1 text-2xl font-semibold text-slate-900">{b.available}<span className="text-sm font-normal text-slate-400"> / {b.entitlement} left</span></p>
            <p className="text-[11px] text-slate-500">Taken {b.taken}{Number(b.pending) ? ` · pending ${b.pending}` : ""}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-900"><CalendarPlus className="h-4 w-4 text-slate-400" />Apply for leave</p>
          <LeaveApplyForm key={formKey} token={token} initial={{ leave_type: "CL", from: "", to: "", reason: "" }}
            onApplied={(approvers) => { toast(`Leave applied - waiting for ${approvers[0] ?? "HR"}`, "success"); setFormKey(formKey + 1); load(); }} />
          <p className="mt-3 text-[11px] text-slate-400">Tip: you can also ask AI Chat, e.g. “sick leave 20 to 22 Oct because fever”.</p>
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-900"><CalendarDays className="h-4 w-4 text-slate-400" />My requests</p>
          {!data ? <div className="h-32 animate-pulse rounded-xl bg-slate-100" /> : data.requests.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">No leave requests yet.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {data.requests.map((l) => (
                <li key={l.id} className="flex items-center justify-between gap-2 py-2">
                  <div className="text-xs">
                    <p className="font-medium text-slate-800">{l.leave_label} · {span(l)} <span className="font-normal text-slate-500">({l.days} day{l.days === "1" ? "" : "s"})</span></p>
                    {l.reason && <p className="text-slate-500">“{l.reason}”</p>}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${TONE[l.status]}`}>{l.status.toLowerCase()}</span>
                    {l.status === "PENDING" && (
                      <button type="button" onClick={async () => {
                        try { await cancelLeave(token, l.id); toast("Request withdrawn", "info"); load(); }
                        catch (e) { toast(e instanceof ApiError ? e.message : "Could not withdraw", "error"); }
                      }} className="text-[11px] text-rose-600 hover:underline">Withdraw</button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {deciding && (
        <ConfirmDialog title={`${deciding.approve ? "Approve" : "Reject"} leave · ${deciding.leave.employee_name}`}
          confirmLabel={deciding.approve ? "Approve" : "Reject"} tone={deciding.approve ? "emerald" : "rose"}
          reasonLabel={deciding.approve ? undefined : "Note to the employee (required)"}
          facts={[{ label: "Leave", value: deciding.leave.leave_label }, { label: "Dates", value: span(deciding.leave) },
            { label: "Days", value: deciding.leave.days }, { label: "Reason", value: deciding.leave.reason ?? "—" }]}
          warning={deciding.approve ? "Approval marks these days as leave in attendance; a processed payslip for the month is recalculated." : undefined}
          onClose={() => setDeciding(null)}
          onConfirm={async (note) => {
            const r = await decideLeave(token, deciding.leave.id, deciding.approve, note);
            toast(deciding.approve ? `Approved${r.recalculated?.length ? ` - payslip recalculated (${r.recalculated.join(", ")})` : ""}` : "Leave rejected",
              deciding.approve ? "success" : "info");
            load();
          }} />
      )}
    </div>
  );
}
