import { CirclePlay, TriangleAlert } from "lucide-react";
import { useState } from "react";
import type { BatchStatus, RegisterRow } from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import { BulkLopCard } from "./BulkLopCard";
import { RunSelectedDialog } from "./RunSelectedDialog";

// Chat card: payroll for the named employees only. Nothing runs until HR confirms in the
// Run selected dialog (LOP + reason); then the batch progress shows here.

const STATUS_TEXT = { PROCESSED: "Processed - will recalculate", LOCKED: "Finalized (locked)", FAILED: "Failed before", NOT_PROCESSED: "Not processed yet" };

export function RunSelectedCard({ card, token }: { card: Record<string, unknown>; token: string }) {
  const month = String(card.month);
  const label = String(card.label);
  const rows = (card.rows as RegisterRow[]) ?? [];
  const skipped = (card.skipped as string[]) ?? [];
  const [open, setOpen] = useState(false);
  const [batch, setBatch] = useState<BatchStatus | null>(null);

  if (batch) return <BulkLopCard batchId={batch.batch_id} token={token} />;
  const locked = rows.filter((r) => r.status === "LOCKED");

  return (
    <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-100 px-4 py-3">
        <p className="text-xs font-medium uppercase tracking-wide text-indigo-700">Run payroll · selected employees</p>
        <h3 className="text-base font-semibold text-slate-900">{label} · {rows.length} employee{rows.length === 1 ? "" : "s"}</h3>
      </div>
      <div className="space-y-3 px-4 py-3 text-sm">
        <ul className="divide-y divide-slate-100 rounded-xl border border-slate-200">
          {rows.map((r) => (
            <li key={r.employee_code} className="flex items-center justify-between gap-3 px-3 py-2">
              <div>
                <span className="font-medium text-slate-900">{r.full_name}</span>
                <span className="block text-xs text-slate-400">{r.employee_code}{r.department ? ` · ${r.department}` : ""}</span>
              </div>
              <div className="text-right text-xs text-slate-600">
                <span className={r.status === "LOCKED" ? "text-rose-600" : ""}>{STATUS_TEXT[r.status]}</span>
                <span className="block text-slate-400">
                  LOP {formatDays(r.lop_days)}{r.net ? ` · net ${formatINR(r.net)}` : ""}
                  {r.attendance_days ? "" : " · no attendance"}
                </span>
              </div>
            </li>
          ))}
        </ul>
        {skipped.length > 0 && (
          <p className="text-xs text-amber-800">Left out (unknown or not employed in {label}): {skipped.join(", ")}</p>
        )}
        {locked.length > 0 && (
          <p className="flex gap-1.5 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />
            Payroll is finalized for {locked.map((r) => r.full_name).join(", ")} - reopen {label} on Payroll Runs first.</p>
        )}
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-slate-500">Only these employees are calculated. Nothing has run yet.</span>
          <button type="button" onClick={() => setOpen(true)} disabled={locked.length > 0}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            <CirclePlay className="h-4 w-4" />Review &amp; run
          </button>
        </div>
      </div>
      {open && (
        <RunSelectedDialog token={token} month={month} label={label} rows={rows}
          onClose={() => setOpen(false)} onDone={setBatch} />
      )}
    </div>
  );
}
