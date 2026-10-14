import { useCan } from "../lib/permissions";
import { CalendarX2, Clock, PencilLine, Plus, TriangleAlert, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";
import {
  ApiError, attendanceDetails, payExtras, type AttendanceDay, type AttendanceDetails, type PayExtras,
} from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import { cancelEntry } from "../lib/api";
import { ChangeDialog, type ChangeKind } from "./ChangeDialog";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "./layout/Toast";

// Display only: every count comes from the attendance service payroll itself uses.

const STATUS: Record<string, { label: string; cell: string; dot: string }> = {
  PRESENT: { label: "Present", cell: "bg-emerald-50 text-emerald-800", dot: "bg-emerald-500" },
  HALF_DAY: { label: "Half day", cell: "bg-amber-50 text-amber-800", dot: "bg-amber-500" },
  ABSENT: { label: "Absent", cell: "bg-rose-50 text-rose-800", dot: "bg-rose-500" },
  LEAVE: { label: "Leave", cell: "bg-sky-50 text-sky-800", dot: "bg-sky-500" },
  HOLIDAY: { label: "Holiday", cell: "bg-violet-50 text-violet-800", dot: "bg-violet-500" },
  WEEK_OFF: { label: "Week off", cell: "bg-slate-100 text-slate-500", dot: "bg-slate-400" },
};
// a day of the month with no attendance record (not entered yet, or still to come)
const NOT_MARKED = { label: "Not marked", cell: "border border-dashed border-slate-300 bg-white text-slate-400", dot: "bg-transparent" };
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** every day of the month ("2026-10"), with the recorded attendance where there is one */
function monthDays(month: string, recorded: AttendanceDay[]): AttendanceDay[] {
  const [y, m] = month.split("-").map(Number);
  const byDate = new Map(recorded.map((d) => [d.date, d]));
  const count = new Date(y, m, 0).getDate();
  return Array.from({ length: count }, (_, i) => {
    const date = `${month}-${String(i + 1).padStart(2, "0")}`;
    return byDate.get(date) ?? { date, status: "NOT_MARKED", remarks: null, lop_days: "0" };
  });
}

function localToday(): string {
  const t = new Date();
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

function Tile({ label, value, tone = "text-slate-900", hint }: {
  label: string; value: string; tone?: string; hint?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-lg font-semibold ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-slate-400">{hint}</p>}
    </div>
  );
}

function Calendar({ month, recorded, onDay }: {
  month: string; recorded: AttendanceDay[]; onDay?: (d: AttendanceDay) => void;
}) {
  const days = monthDays(month, recorded);
  const today = localToday();
  const missing = days.filter((d) => d.status === "NOT_MARKED" && d.date <= today).length;
  const upcoming = days.filter((d) => d.date > today).length;
  // Monday-first grid: blank cells before the first day of the month
  const lead = (new Date(`${days[0].date}T00:00:00`).getDay() + 6) % 7;
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="grid grid-cols-7 gap-1.5 text-center text-[11px] font-medium text-slate-400">
        {WEEKDAYS.map((d) => <span key={d}>{d}</span>)}
      </div>
      <div className="mt-1.5 grid grid-cols-7 gap-1.5">
        {Array.from({ length: lead }, (_, i) => <span key={`b${i}`} />)}
        {days.map((d) => {
          const s = d.status === "NOT_MARKED" ? NOT_MARKED : STATUS[d.status] ?? STATUS.WEEK_OFF;
          const lop = Number(d.lop_days) > 0;
          const future = d.date > today;
          const editable = !!onDay && !future; // attendance of days still to come is not entered
          return (
            <button type="button" key={d.date} disabled={!editable} onClick={() => onDay?.(d)}
              title={`${d.date} · ${future ? "Upcoming" : s.label}${lop ? ` · LOP ${formatDays(d.lop_days)}` : ""}${d.remarks ? ` · ${d.remarks}` : ""}${editable ? " · click to edit" : ""}`}
              className={`relative flex h-11 flex-col items-center justify-center rounded-lg text-xs font-medium ${s.cell} ${future ? "opacity-50" : ""} ${lop ? "ring-2 ring-rose-400" : ""} ${editable ? "cursor-pointer hover:ring-2 hover:ring-indigo-400" : "cursor-default"}`}>
              {Number(d.date.slice(8))}
              <span className={`mt-0.5 h-1.5 w-1.5 rounded-full ${s.dot}`} />
              {lop && <span className="absolute right-0.5 top-0.5 rounded bg-rose-500 px-1 text-[8px] font-bold text-white">LOP</span>}
            </button>
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        {Object.values(STATUS).map((s) => (
          <span key={s.label} className="flex items-center gap-1.5"><span className={`h-2 w-2 rounded-full ${s.dot}`} />{s.label}</span>
        ))}
        <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded ring-2 ring-rose-400" />LOP day</span>
        <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded border border-dashed border-slate-400" />Not marked</span>
      </div>
      {(missing > 0 || upcoming > 0) && (
        <p className="mt-2 text-[11px] text-slate-500">
          {missing > 0 && <>{missing} day(s) up to today have no attendance yet - click a day to mark it, or use "Add missing days". </>}
          {upcoming > 0 && <>{upcoming} upcoming day(s) are shown faded.</>}
        </p>
      )}
    </div>
  );
}

export function AttendanceTab({ token, code, month, onExpired, canEdit = false, employeeName = code }: {
  token: string; code: string; month: string; onExpired: () => void;
  /** HR: show the change actions (the backend checks the role again) */
  canEdit?: boolean; employeeName?: string;
}) {
  const [data, setData] = useState<AttendanceDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [extras, setExtras] = useState<PayExtras | null>(null);
  const [dialog, setDialog] = useState<ChangeKind | null>(null);
  const [dayEdit, setDayEdit] = useState<{ date: string; status: string; remarks: string | null } | null>(null);
  const [fillOpen, setFillOpen] = useState(false);
  const [cancelling, setCancelling] = useState<{ kind: "overtime" | "adjustment"; id: number; text: string } | null>(null);
  const [reload, setReload] = useState(0);
  const toast = useToast();
  const can = useCan();

  useEffect(() => {
    let live = true;
    payExtras(token, code, month).then((d) => { if (live) setExtras(d); }).catch(() => setExtras(null));
    return () => { live = false; };
  }, [token, code, month, reload]);

  useEffect(() => {
    let live = true;
    setError(null);
    attendanceDetails(token, code, month)
      .then((d) => { if (live) setData(d); })
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 401) return onExpired();
        setError(e instanceof ApiError ? e.message : "Could not load attendance");
      });
    return () => { live = false; };
  }, [token, code, month, onExpired, reload]);

  if (error) return <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>;
  if (!data) return <div className="h-40 animate-pulse rounded-xl bg-slate-200/60" />;
  if (!data.available) {
    return (
      <div className="flex flex-col items-center rounded-xl border border-dashed border-slate-300 p-8 text-center">
        <CalendarX2 className="h-8 w-8 text-slate-300" />
        <p className="mt-2 text-sm text-slate-500">{data.message}</p>
        {canEdit && (
          <button type="button" onClick={() => setFillOpen(true)}
            className="mt-3 inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700">
            <Plus className="h-3.5 w-3.5" />Create attendance for {data.month_label}
          </button>
        )}
        {fillOpen && (
          <ChangeDialog token={token} kind="attendance" employeeCode={code} employeeName={employeeName} month={data.month}
            initial={{ fill_month: true, reason: "" }} onClose={() => setFillOpen(false)} onApplied={() => setReload((k) => k + 1)} />
        )}
      </div>
    );
  }

  const s = data.summary!;
  const lopTone = Number(s.lop_days) > 0 ? "text-rose-600" : "text-slate-900";
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile label="Total days" value={String(s.calendar_days)} hint={`${formatDays(s.week_offs)} week offs`} />
        <Tile label="Working days" value={formatDays(s.working_days)} hint={`${formatDays(s.holidays)} holiday(s)`} />
        <Tile label="Present" value={formatDays(s.present_days)} tone="text-emerald-700" />
        <Tile label="Paid leave" value={formatDays(s.paid_leave_days)} tone="text-sky-700" />
        <Tile label="Absent" value={formatDays(s.absent_days)}
          hint={Number(s.unapproved_absences) ? `${formatDays(s.unapproved_absences)} without approved leave` : undefined} />
        <Tile label="LOP" value={formatDays(s.lop_days)} tone={lopTone} />
        <Tile label="Paid days" value={`${formatDays(s.paid_days)} / ${formatDays(s.basis_days)}`} tone="text-indigo-700"
          hint={`${s.basis.toLowerCase()} basis`} />
        <Tile label="Overtime"
          value={extras && !extras.restricted ? `${formatDays(extras.hours!)} h` : "—"}
          hint={extras && !extras.restricted
            ? (extras.rate ? `${formatINR(extras.amount!)} · rate ${formatINR(extras.rate.rate_per_hour)}/h` : extras.rate_error ?? undefined)
            : "Visible to HR and the employee"} />
      </div>

      {data.warnings!.length > 0 && (
        <div className="space-y-1 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
          {data.warnings!.map((w) => <p key={w} className="flex gap-1.5"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />{w}</p>)}
        </div>
      )}

      {extras && !extras.restricted && (extras.overtime!.length > 0 || extras.adjustments!.length > 0 || extras.rate || canEdit) && (
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-sm font-semibold text-slate-900">Overtime &amp; one-off pay · {extras.month_label}</p>
            {extras.rate && (
              <p className="text-[11px] text-slate-500" title={extras.rate.steps.map((st) => `${st.label}: ${st.formula ?? ""} = ${st.result}`).join(" · ")}>
                Rate {formatINR(extras.rate.rate_per_hour)}/h · {extras.rate.source} · {extras.rate.formula}
              </p>
            )}
          </div>
          {extras.overtime!.length === 0 && extras.adjustments!.length === 0 ? (
            <p className="mt-2 text-xs text-slate-500">No overtime or one-off pay this month.</p>
          ) : (
            <ul className="mt-2 divide-y divide-slate-100 text-xs">
              {extras.overtime!.map((o) => (
                <li key={`o${o.id}`} className="flex justify-between gap-3 py-2">
                  <span className="text-slate-600">Overtime {o.work_date}<span className="block text-[11px] text-slate-400">{o.formula}</span></span>
                  <span className="flex items-center gap-2">
                    <span className="font-medium tabular-nums text-slate-900">{formatINR(o.amount)}</span>
                    {canEdit && <button type="button" title="Cancel this entry" onClick={() => setCancelling({ kind: "overtime", id: o.id, text: `Overtime ${o.work_date}: ${o.formula}` })}
                      className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"><Undo2 className="h-3.5 w-3.5" /></button>}
                  </span>
                </li>
              ))}
              {extras.adjustments!.map((a) => (
                <li key={`a${a.id}`} className="flex justify-between gap-3 py-2">
                  <span className="text-slate-600">{a.label ?? a.head_code} <span className="text-slate-400">({a.kind.toLowerCase()})</span>
                    <span className="block text-[11px] text-slate-400">{a.reason}</span></span>
                  <span className={`font-medium tabular-nums ${a.kind === "DEDUCTION" ? "text-rose-600" : "text-slate-900"}`}>
                    {a.kind === "DEDUCTION" ? "−" : ""}{formatINR(a.amount)}
                    {canEdit && <button type="button" title="Cancel this entry" onClick={() => setCancelling({ kind: "adjustment", id: a.id, text: `${a.label ?? a.head_code} ${formatINR(a.amount)}` })}
                      className="ml-2 rounded p-1 align-middle text-slate-400 hover:bg-rose-50 hover:text-rose-600"><Undo2 className="h-3.5 w-3.5" /></button>}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {canEdit && (
            <div className="mt-3 flex flex-wrap gap-2">
              <button type="button" onClick={() => setDialog("overtime")}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"><Plus className="h-3.5 w-3.5" />Add overtime</button>
              {can("edit_pay") && <button type="button" onClick={() => setDialog("adjustment")}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"><Plus className="h-3.5 w-3.5" />Add one-off pay</button>}
            </div>
          )}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="space-y-2 lg:col-span-3">
          <Calendar month={data.month} recorded={data.days!} onDay={canEdit ? (d) => setDayEdit({ date: d.date, status: d.status === "LEAVE" || d.status === "NOT_MARKED" ? "PRESENT" : d.status, remarks: d.remarks }) : undefined} />
          {canEdit && (
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
              <span>Click a day to correct its attendance.</span>
              <span className="flex gap-2">
                <button type="button" onClick={() => setDayEdit({ date: data.days!.at(-1)?.date ?? "", status: "PRESENT", remarks: null })}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"><PencilLine className="h-3.5 w-3.5" />Edit attendance</button>
                {data.days!.length < s.calendar_days && (
                  <button type="button" onClick={() => setFillOpen(true)}
                    className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"><Plus className="h-3.5 w-3.5" />Add missing days</button>
                )}
              </span>
            </div>
          )}
        </div>
        <div className="space-y-4 lg:col-span-2">
          <div className="rounded-xl border border-slate-200 bg-white p-4">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold text-slate-900">LOP decision</p>
              {canEdit && (
                <button type="button" onClick={() => setDialog("lop")}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50">
                  <PencilLine className="h-3.5 w-3.5" /> Edit LOP
                </button>
              )}
            </div>
            {data.lop ? (
              <div className="mt-2 text-xs text-slate-600">
                <p><span className="text-lg font-semibold text-slate-900">{formatDays(data.lop.days)}</span> day(s)</p>
                <p className="mt-1">{data.lop.reason}</p>
                {data.lop.updated_by && <p className="mt-1 text-slate-400">By {data.lop.updated_by}</p>}
              </div>
            ) : (
              <p className="mt-2 text-xs text-slate-500">No LOP recorded by HR for {data.month_label}.</p>
            )}
            <p className="mt-3 border-t border-slate-100 pt-2 text-xs text-slate-500">
              {data.payroll
                ? <>Payroll {data.payroll.status.toLowerCase()}: paid {formatDays(data.payroll.paid_days)} days, LOP {formatDays(data.payroll.lop_days)}</>
                : <>Payroll not processed for {data.month_label} yet.</>}
            </p>
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4">
            <p className="flex items-center gap-1.5 text-sm font-semibold text-slate-900"><Clock className="h-4 w-4 text-slate-400" />How paid days were worked out</p>
            <p className="mt-1 text-[11px] text-slate-400">{data.paid_days_formula}</p>
            <ol className="mt-3 space-y-2 text-xs">
              {data.paid_days_steps!.map((st) => (
                <li key={st.label} className="flex justify-between gap-3">
                  <span className="text-slate-600">{st.label}{st.formula && <span className="block text-[11px] text-slate-400">{st.formula}</span>}</span>
                  <span className="font-medium tabular-nums text-slate-900">{st.result != null ? formatDays(st.result) : ""}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
      {dialog && (
        <ChangeDialog token={token} kind={dialog} employeeCode={code} employeeName={employeeName} month={data.month}
          initial={dialog === "lop" ? { lop_days: s.lop_days, reason: "" } : undefined}
          onClose={() => setDialog(null)} onApplied={() => setReload((k) => k + 1)} />
      )}
      {dayEdit && (
        <ChangeDialog token={token} kind="attendance" employeeCode={code} employeeName={employeeName} month={data.month}
          initial={{ attendance: [dayEdit], reason: "" }} currentLop={s.lop_days}
          onClose={() => setDayEdit(null)} onApplied={() => setReload((k) => k + 1)} />
      )}
      {fillOpen && (
        <ChangeDialog token={token} kind="attendance" employeeCode={code} employeeName={employeeName} month={data.month}
          initial={{ fill_month: true, reason: "" }} onClose={() => setFillOpen(false)} onApplied={() => setReload((k) => k + 1)} />
      )}
      {cancelling && (
        <ConfirmDialog title="Cancel entry" confirmLabel="Cancel entry" tone="rose" reasonLabel="Reason (required)"
          facts={[{ label: "Entry", value: cancelling.text }, { label: "Month", value: data.month_label }]}
          warning="The entry is kept in the history as cancelled and the payslip is recalculated if payroll is processed."
          onClose={() => setCancelling(null)}
          onConfirm={async (reason) => {
            const r = await cancelEntry(token, cancelling.kind, cancelling.id, reason);
            toast(`Entry cancelled${r.recalculated.length ? " · payslip recalculated" : ""}`, "success");
            setReload((k) => k + 1);
          }} />
      )}
    </div>
  );
}
