import { useCan } from "../lib/permissions";
import { CalendarDays, CirclePlay, Gift, Lock, PencilLine, Search, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { monthEmployees, type RegisterRow } from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import { ChangeDialog, type ChangeKind } from "./ChangeDialog";
import { RunSelectedDialog } from "./RunSelectedDialog";
import { useToast } from "./layout/Toast";

// Employees of the payroll month with their payslip; HR edits LOP, one-off pay (bonus /
// miscellaneous) and attendance from here. Every change previews first and goes through
// the approval setting; a processed payslip is recalculated when the change is applied.

const STATUS = {
  PROCESSED: { label: "Processed", tone: "bg-sky-50 text-sky-700" },
  LOCKED: { label: "Locked", tone: "bg-emerald-600 text-white" },
  FAILED: { label: "Failed", tone: "bg-rose-50 text-rose-700" },
  NOT_PROCESSED: { label: "Not processed", tone: "bg-slate-100 text-slate-600" },
};

export function PayrollRegister({ token, month, label, refreshKey, onChanged, canRun = true }: {
  token: string; month: string; label: string; refreshKey: number; onChanged: () => void;
  /** false while another batch of the month is being reviewed or processed */
  canRun?: boolean;
}) {
  const toast = useToast();
  const can = useCan();
  // selection survives paging and search: code -> row (for the confirmation summary)
  const [selected, setSelected] = useState<Map<string, RegisterRow>>(new Map());
  const [confirmRun, setConfirmRun] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Awaited<ReturnType<typeof monthEmployees>> | null>(null);
  const [edit, setEdit] = useState<{ row: RegisterRow; kind: ChangeKind } | null>(null);
  const [tick, setTick] = useState(0);

  const load = useCallback(() => monthEmployees(token, month, search, page).then(setData).catch(() => undefined),
    [token, month, search, page]);
  useEffect(() => { setPage(1); }, [month, search]);
  useEffect(() => { setSelected(new Map()); }, [month]);

  const toggle = (r: RegisterRow) => setSelected((cur) => {
    const next = new Map(cur);
    if (next.has(r.employee_code)) next.delete(r.employee_code); else next.set(r.employee_code, r);
    return next;
  });
  const selectable = (data?.items ?? []).filter((r) => r.status !== "LOCKED");
  const allOnPage = selectable.length > 0 && selectable.every((r) => selected.has(r.employee_code));
  const togglePage = () => setSelected((cur) => {
    const next = new Map(cur);
    selectable.forEach((r) => (allOnPage ? next.delete(r.employee_code) : next.set(r.employee_code, r)));
    return next;
  });
  const chosen = [...selected.values()];
  useEffect(() => { void load(); }, [load, refreshKey, tick]);

  const action = (row: RegisterRow, kind: ChangeKind, icon: React.ReactNode, text: string) => {
    const allowed = can(kind === "adjustment" || kind === "salary" ? "edit_pay" : "edit_attendance");
    if (!allowed) return null;
    return (
    <button type="button" disabled={row.status === "LOCKED"} onClick={() => setEdit({ row, kind })}
      title={row.status === "LOCKED" ? "Payslip is locked (month finalized)" : text}
      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-indigo-300 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-40">
      {icon}{text}
    </button>
    );
  };

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-900">Employees · {label}</p>
          <p className="text-xs text-slate-500">Edit LOP, add bonus / miscellaneous pay or correct attendance for anyone in this month.</p>
        </div>
        <label className="flex items-center gap-2 rounded-lg border border-slate-300 px-2.5 focus-within:border-indigo-500">
          <Search className="h-3.5 w-3.5 text-slate-400" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name or ID" maxLength={100}
            className="h-8 w-44 bg-transparent text-sm outline-none" />
        </label>
      </div>

      {selected.size > 0 && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-indigo-50 px-3 py-2 text-sm">
          <span className="text-indigo-900"><strong>{selected.size}</strong> employee{selected.size === 1 ? "" : "s"} selected
            <button type="button" onClick={() => setSelected(new Map())} className="ml-2 inline-flex items-center gap-0.5 text-xs text-indigo-700 hover:underline"><X className="h-3 w-3" />clear</button></span>
          <button type="button" onClick={() => setConfirmRun(true)} disabled={!canRun}
            title={canRun ? undefined : "Another batch of this month is still running or in LOP review"}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            <CirclePlay className="h-3.5 w-3.5" />Run payroll for selected ({selected.size})
          </button>
        </div>
      )}

      {!data ? <div className="mt-4 h-40 animate-pulse rounded-xl bg-slate-100" /> : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[860px] text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wide text-slate-500">
              <tr><th className="w-8 py-2"><input type="checkbox" aria-label="Select all on this page" checked={allOnPage} onChange={togglePage} /></th>
                <th className="py-2">Employee</th><th>Status</th><th className="text-right">Attendance</th><th className="text-right">LOP</th>
                <th className="text-right">Gross</th><th className="text-right">Net pay</th><th className="pl-4">Change</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.items.map((r) => (
                <tr key={r.employee_code} className={`align-top ${selected.has(r.employee_code) ? "bg-indigo-50/50" : ""}`}>
                  <td className="py-2.5"><input type="checkbox" aria-label={`Select ${r.full_name}`} disabled={r.status === "LOCKED"}
                    checked={selected.has(r.employee_code)} onChange={() => toggle(r)} /></td>
                  <td className="py-2.5"><span className="font-medium text-slate-900">{r.full_name}</span>
                    <span className="block text-xs text-slate-400">{r.employee_code} · {r.department ?? "—"}</span>
                    {r.error && <span className="mt-0.5 flex gap-1 text-[11px] text-rose-700"><TriangleAlert className="h-3 w-3 shrink-0" />{r.error}</span>}
                  </td>
                  <td className="py-2.5"><span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold ${STATUS[r.status].tone}`}>
                    {r.status === "LOCKED" && <Lock className="mr-1 inline h-3 w-3" />}{STATUS[r.status].label}</span>
                    {(r.one_off > 0 || r.overtime > 0) && <span className="block pt-1 text-[10px] text-slate-400">{r.one_off ? `${r.one_off} one-off` : ""}{r.one_off && r.overtime ? " · " : ""}{r.overtime ? `${r.overtime} overtime` : ""}</span>}
                  </td>
                  <td className="py-2.5 text-right tabular-nums text-slate-600">{r.attendance_days ? `${r.attendance_days} days` : <span className="text-rose-600">none</span>}</td>
                  <td className="py-2.5 text-right tabular-nums">{formatDays(r.lop_days)}</td>
                  <td className="py-2.5 text-right tabular-nums">{r.gross ? formatINR(r.gross) : "—"}</td>
                  <td className="py-2.5 text-right font-medium tabular-nums">{r.net ? formatINR(r.net) : "—"}</td>
                  <td className="py-2.5 pl-4">
                    <div className="flex flex-wrap gap-1">
                      {action(r, "lop", <PencilLine className="h-3 w-3" />, "LOP")}
                      {action(r, "adjustment", <Gift className="h-3 w-3" />, "Bonus / misc")}
                      {action(r, "attendance", <CalendarDays className="h-3 w-3" />, "Attendance")}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.items.length === 0 && <p className="py-6 text-center text-sm text-slate-500">No employees match.</p>}
        </div>
      )}
      {data && data.pages > 1 && (
        <div className="flex items-center justify-between pt-2 text-xs text-slate-600">
          <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Prev</button>
          <span>Page {data.page} of {data.pages} · {data.total} employees</span>
          <button type="button" disabled={page >= data.pages} onClick={() => setPage(page + 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next ›</button>
        </div>
      )}

      {confirmRun && (
        <RunSelectedDialog token={token} month={month} label={label} rows={chosen} onClose={() => setConfirmRun(false)}
          onDone={(b) => {
            toast(`Payroll queued for ${b.total} employee${b.total === 1 ? "" : "s"} - progress is shown above`, "success");
            setSelected(new Map());
            setTick((t) => t + 1);
            onChanged();
          }} />
      )}
      {edit && (
        <ChangeDialog token={token} kind={edit.kind} employeeCode={edit.row.employee_code} employeeName={edit.row.full_name}
          month={month} currentLop={edit.row.lop_days}
          initial={edit.kind === "lop" ? { lop_days: edit.row.lop_days, reason: "" }
            : edit.kind === "adjustment" ? { adjustments: [{ kind: "EARNING", head_code: "BONUS", amount: "" }], reason: "" } : undefined}
          onClose={() => setEdit(null)} onApplied={() => { setTick((t) => t + 1); onChanged(); }} />
      )}
    </section>
  );
}
