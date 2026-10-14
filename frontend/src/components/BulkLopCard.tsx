import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  batchStatus,
  downloadFile,
  lopItems,
  patchLopItem,
  retryFailed,
  runBatch,
  type BatchStatus,
  type LopItem,
  type LopPage,
} from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import { DownloadIcon } from "./icons";

const STEP = 0.5;

function shortDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

function Tile({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`rounded-xl px-3 py-2 ${tone}`}>
      <p className="text-[11px] font-medium uppercase tracking-wide opacity-80">{label}</p>
      <p className="text-lg font-semibold tabular-nums">{value.toLocaleString("en-IN")}</p>
    </div>
  );
}

function scopeLabel(filters: Record<string, string>): string {
  const parts = [filters.department, filters.work_state].filter(Boolean);
  const scope = parts.length ? parts.join(" · ") : "All employees";
  const picked = (filters as Record<string, unknown>).employees;
  if (Array.isArray(picked)) return `Selected employees (${picked.length})`;
  return (filters as Record<string, unknown>).recalculate ? `Recalculation · ${scope}` : scope;
}

export function BulkLopCard({ batchId, token, onRun, onFinished }: {
  batchId: string; token: string;
  /** set by the payroll run card: the run goes through its approval step instead */
  onRun?: (mode: "APPLY" | "SKIP_ALL", reason: string) => Promise<void>;
  /** called once when the batch has finished calculating */
  onFinished?: () => void;
}) {
  const [status, setStatus] = useState<BatchStatus | null>(null);
  const [page, setPage] = useState<LopPage | null>(null);
  const [pageNo, setPageNo] = useState(1);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(new Map<string, number>()); // latest edit per row wins

  const fail = (err: unknown) =>
    setError(err instanceof ApiError
      ? (err.status === 401 ? "Session expired - please log out and sign in again." : err.message)
      : "Something went wrong.");

  // load status (decides review vs progress view)
  useEffect(() => {
    batchStatus(token, batchId).then(setStatus).catch(fail);
  }, [token, batchId]);

  // debounce the search box
  useEffect(() => {
    const t = setTimeout(() => { setQuery(search); setPageNo(1); }, 300);
    return () => clearTimeout(t);
  }, [search]);

  const inReview = status?.status === "REVIEW";
  const loadPage = useCallback(() => {
    if (!inReview) return;
    lopItems(token, batchId, pageNo, query).then(setPage).catch(fail);
  }, [token, batchId, pageNo, query, inReview]);
  useEffect(loadPage, [loadPage]);

  // poll progress every 2 s while running
  const running = status !== null && !status.finished && status.status !== "REVIEW";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      batchStatus(token, batchId).then(setStatus).catch(fail);
    }, 2000);
    return () => clearInterval(timer);
  }, [running, token, batchId]);

  // tell the payroll run card once the batch has finished
  const reported = useRef(false);
  useEffect(() => {
    if (status?.finished && onFinished && !reported.current) {
      reported.current = true;
      onFinished();
    }
  }, [status?.finished, onFinished]);

  function change(item: LopItem, edit: { apply?: boolean; lop_days?: string }) {
    setError(null);
    const local: LopItem = { ...item, ...edit };
    local.deduction = String(Math.round(Number(local.per_day) * Number(local.lop_days)));
    setPage((p) => p && { ...p, items: p.items.map((i) => (i.emp_id === item.emp_id ? local : i)) });
    const ticket = (saving.current.get(item.emp_id) ?? 0) + 1;
    saving.current.set(item.emp_id, ticket);
    patchLopItem(token, batchId, item.emp_id, edit)
      .then(({ item: saved, totals }) => {
        if (saving.current.get(item.emp_id) !== ticket) return; // a newer edit is on its way
        setPage((p) => p && { ...p, totals,
          items: p.items.map((i) => (i.emp_id === saved.emp_id ? saved : i)) });
      })
      .catch((err) => { fail(err); loadPage(); });
  }

  function step(item: LopItem, delta: number) {
    const next = Math.min(Number(item.absent_days), Math.max(0, Number(item.lop_days) + delta));
    if (next !== Number(item.lop_days)) change(item, { lop_days: String(next) });
  }

  async function start(mode: "APPLY" | "SKIP_ALL") {
    if (submitting) return; // the backend also refuses a second run
    setSubmitting(true);
    setError(null);
    try {
      if (onRun) {
        await onRun(mode, mode === "APPLY" ? reason : "");
        setStatus(await batchStatus(token, batchId));
      } else {
        setStatus(await runBatch(token, batchId, mode, mode === "APPLY" ? reason : ""));
      }
    } catch (err) {
      fail(err);
      batchStatus(token, batchId).then(setStatus).catch(() => undefined);
    } finally {
      setSubmitting(false);
    }
  }

  async function retry() {
    setError(null);
    try { setStatus(await retryFailed(token, batchId)); } catch (err) { fail(err); }
  }

  if (!status) {
    return <div className="text-sm text-slate-500">{error ?? "Loading payroll batch…"}</div>;
  }

  const header = (
    <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3">
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-indigo-700">Bulk payroll</p>
        <h3 className="text-base font-semibold text-slate-900">
          {status.period} · {scopeLabel(status.filters)}
        </h3>
      </div>
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
        {status.status.replaceAll("_", " ").toLowerCase()}
      </span>
    </div>
  );

  // ------------------------------------------------------------ progress / result view
  if (!inReview) {
    const finished = status.finished;
    return (
      <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
        {header}
        <div className="space-y-3 px-4 py-4">
          <div>
            <div className="mb-1 flex justify-between text-xs text-slate-600">
              <span>{finished ? "Finished" : "Processing payroll…"}</span>
              <span className="tabular-nums">
                {status.processed.toLocaleString("en-IN")} / {status.total.toLocaleString("en-IN")}
              </span>
            </div>
            <div className="h-2.5 overflow-hidden rounded-full bg-slate-100" role="progressbar"
              aria-valuenow={status.percent} aria-valuemin={0} aria-valuemax={100}>
              <div className={`h-full rounded-full transition-all ${status.failed ? "bg-amber-500" : "bg-emerald-600"}`}
                style={{ width: `${status.percent}%` }} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Tile label="Processed" value={status.done} tone="bg-emerald-50 text-emerald-900" />
            <Tile label="LOP applied" value={status.lop_applied} tone="bg-indigo-50 text-indigo-900" />
            <Tile label="Already done" value={status.skipped} tone="bg-slate-50 text-slate-800" />
            <Tile label="Failed" value={status.failed} tone={status.failed ? "bg-red-50 text-red-800" : "bg-slate-50 text-slate-800"} />
          </div>
          {finished && (
            <p className="text-sm text-slate-700">
              {status.failed
                ? `Payroll finished with ${status.failed} error(s). Download the report to see why, fix the data and retry the failed ones.`
                : "Payroll processed for everyone in this batch."}
            </p>
          )}
          {finished && (
            <div className="flex flex-wrap gap-2">
              <button type="button"
                onClick={() => downloadFile(token, `/batches/${batchId}/report.csv`, `payroll_${status.period}_report.csv`).catch(fail)}
                className="flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
                <DownloadIcon /> Download report (CSV)
              </button>
              {status.can_retry_failed && (
                <button type="button" onClick={retry}
                  className="rounded-lg bg-amber-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600">
                  Retry failed only
                </button>
              )}
            </div>
          )}
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------ review view
  const totals = page?.totals;
  const lopSelected = Number(totals?.total_lop_days ?? 0) > 0;
  return (
    <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      {header}
      <div className="grid grid-cols-3 gap-2 px-4 pt-3">
        <Tile label="Total employees" value={status.total} tone="bg-slate-50 text-slate-800" />
        <Tile label="No issues" value={status.ok_count + status.already_processed} tone="bg-emerald-50 text-emerald-900" />
        <Tile label="Needs LOP review" value={status.review_count} tone="bg-amber-50 text-amber-900" />
      </div>

      {status.review_count > 0 && (
        <div className="px-4 pt-3">
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, ID or department"
            className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-500">
                  <th className="py-2 pr-2 font-medium">LOP</th>
                  <th className="py-2 pr-2 font-medium">Employee</th>
                  <th className="py-2 pr-2 font-medium">Absent (no leave)</th>
                  <th className="py-2 pr-2 font-medium">LOP days</th>
                  <th className="py-2 text-right font-medium">Deduction</th>
                </tr>
              </thead>
              <tbody>
                {page?.items.map((item) => (
                  <tr key={item.emp_id} className={`border-b border-slate-100 ${item.apply ? "" : "opacity-50"}`}>
                    <td className="py-2 pr-2">
                      <input type="checkbox" checked={item.apply} aria-label={`Apply LOP for ${item.name}`}
                        onChange={(e) => change(item, { apply: e.target.checked })}
                        className="h-4 w-4 accent-indigo-600" />
                    </td>
                    <td className="py-2 pr-2">
                      <p className="font-medium text-slate-800">{item.name}</p>
                      <p className="text-xs text-slate-500">{item.emp_id}{item.department ? ` · ${item.department}` : ""}</p>
                    </td>
                    <td className="py-2 pr-2 text-xs text-slate-600">
                      {item.absent_dates.map(shortDate).join(", ")}
                    </td>
                    <td className="py-2 pr-2">
                      <div className="flex items-center gap-1">
                        <button type="button" aria-label="One day less" disabled={!item.apply || Number(item.lop_days) <= 0}
                          onClick={() => step(item, -STEP)}
                          className="h-7 w-7 rounded-md border border-slate-300 text-slate-700 hover:bg-slate-100 disabled:opacity-40">−</button>
                        <span className="w-8 text-center tabular-nums">{formatDays(item.lop_days)}</span>
                        <button type="button" aria-label="One day more" disabled={!item.apply || Number(item.lop_days) >= Number(item.absent_days)}
                          onClick={() => step(item, STEP)}
                          className="h-7 w-7 rounded-md border border-slate-300 text-slate-700 hover:bg-slate-100 disabled:opacity-40">+</button>
                      </div>
                    </td>
                    <td className="py-2 text-right tabular-nums text-slate-800">
                      {item.apply ? formatINR(item.deduction) : "—"}
                    </td>
                  </tr>
                ))}
                {page && page.items.length === 0 && (
                  <tr><td colSpan={5} className="py-4 text-center text-sm text-slate-500">No employees match "{query}"</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {page && page.pages > 1 && (
            <div className="mt-2 flex items-center justify-between text-xs text-slate-600">
              <button type="button" disabled={pageNo <= 1} onClick={() => setPageNo((n) => n - 1)}
                className="rounded-md px-2 py-1 hover:bg-slate-100 disabled:opacity-40">← Previous</button>
              <span>Page {page.page} of {page.pages} · {page.total_items} employees</span>
              <button type="button" disabled={pageNo >= page.pages} onClick={() => setPageNo((n) => n + 1)}
                className="rounded-md px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next →</button>
            </div>
          )}
        </div>
      )}

      <div className="mt-3 space-y-3 border-t border-slate-100 bg-slate-50 px-4 py-3">
        {status.review_count > 0 ? (
          <>
            <div className="flex items-center justify-between text-sm">
              <span className="text-slate-600">
                LOP for {totals?.selected ?? 0} employee(s), {formatDays(totals?.total_lop_days ?? "0")} day(s)
              </span>
              <span className="font-semibold tabular-nums text-slate-900">
                Total deduction {formatINR(totals?.total_deduction ?? "0")}
              </span>
            </div>
            <label className="block">
              <span className="text-xs font-medium text-slate-700">Reason for LOP (required)</span>
              <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500}
                placeholder="e.g. Absent without approved leave in September"
                className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
            </label>
          </>
        ) : (
          <p className="text-sm text-slate-700">No unapproved absences - nobody needs LOP. Payroll can run for everyone.</p>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          {status.review_count > 0 && (
            <button type="button" disabled={submitting} onClick={() => start("SKIP_ALL")}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50">
              Skip all LOP
            </button>
          )}
          <button type="button"
            disabled={submitting || (status.review_count > 0 && lopSelected && reason.trim().length < 5)}
            title={lopSelected && reason.trim().length < 5 ? "Enter a reason (at least 5 characters)" : undefined}
            onClick={() => start(status.review_count > 0 ? "APPLY" : "SKIP_ALL")}
            className="rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            {submitting ? "Starting…" : onRun
              ? (status.review_count > 0 ? "Approve LOP & run payroll" : "Approve & run payroll")
              : status.review_count > 0 ? "Apply LOP and run payroll" : "Run payroll"}
          </button>
        </div>
      </div>
    </div>
  );
}
