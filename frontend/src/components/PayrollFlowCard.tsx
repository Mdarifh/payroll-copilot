import { useCallback, useEffect, useState } from "react";
import { ArrowRight, Check, CircleX, Loader2, RefreshCw, TriangleAlert } from "lucide-react";
import {
  ApiError,
  getPayrollFlow,
  resumePayrollFlow,
  type FlowDecision,
  type PayrollFlowCard as Flow,
} from "../lib/api";
import { formatINR } from "../lib/format";
import type { PageId } from "../lib/nav";
import { BulkLopCard } from "./BulkLopCard";

const STEP_LABELS: Record<string, string> = {
  prechecks: "Pre-checks", review: "LOP review", approval: "Your approval",
  run: "Run", validate: "Validate", summary: "Summary",
};

/** how far the run is, as an index into flow.steps */
function reached(flow: Flow): number {
  if (flow.stage === "done") return 6;
  if (flow.stage === "running") return 3;
  if (flow.stage === "approval") return flow.batch_id ? 2 : 0;
  return 2;
}

function Stepper({ flow }: { flow: Flow }) {
  const at = reached(flow);
  return (
    <ol className="flex flex-wrap items-center gap-1 text-[11px]">
      {flow.steps.map((step, i) => {
        const done = i < at;
        const current = i === at && flow.stage !== "cancelled";
        return (
          <li key={step} className="flex items-center gap-1">
            <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 font-medium ${
              done ? "bg-emerald-50 text-emerald-700"
                : current ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-500"}`}>
              {done && <Check className="h-3 w-3" />}
              {current && flow.stage === "running" && <Loader2 className="h-3 w-3 animate-spin" />}
              {STEP_LABELS[step] ?? step}
            </span>
            {i < flow.steps.length - 1 && <span className="text-slate-300">›</span>}
          </li>
        );
      })}
    </ol>
  );
}

const HEADLINE: Record<Flow["stage"], string> = {
  approval: "needs your approval", running: "calculating", done: "calculated",
  cancelled: "cancelled", error: "needs attention",
};

const STATUS_STYLE: Record<string, string> = {
  PASS: "text-emerald-700", WARNING: "text-amber-700", FAIL: "text-red-700",
};
const STATUS_TEXT: Record<string, string> = { PASS: "✓ OK", WARNING: "⚠ Warning", FAIL: "✗ Failed" };

function Prechecks({ flow }: { flow: Flow }) {
  const pc = flow.prechecks;
  if (!pc) return null;
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-slate-700">
        Pre-checks · {pc.employees} employees{pc.already_processed ? ` · ${pc.already_processed} already processed` : ""}
      </p>
      <dl className="grid grid-cols-2 gap-x-4 text-xs sm:grid-cols-3">
        {pc.checks.map((c) => (
          <div key={c.key} className="flex justify-between gap-2 border-b border-slate-100 py-1">
            <dt className="text-slate-600">{c.label}</dt>
            <dd className={`font-medium ${STATUS_STYLE[c.status]}`}>{STATUS_TEXT[c.status]}</dd>
          </div>
        ))}
      </dl>
      {pc.notes.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-[11px] text-slate-500">{pc.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      )}
    </div>
  );
}

function Blockers({ flow }: { flow: Flow }) {
  if (!flow.blockers.length) return null;
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
      <p className="mb-1 flex items-center gap-1.5 font-semibold"><CircleX className="h-4 w-4" />
        Payroll cannot run yet - fix these, then press Re-check</p>
      {flow.blockers.map((b) => (
        <div key={b.check} className="mt-1">
          <p className="font-medium">{b.check} · {b.count} failed</p>
          <ul className="ml-4 list-disc">
            {b.employees.map((e) => <li key={e.code}>{e.name} ({e.code}): {e.message}</li>)}
            {b.count > b.employees.length && <li>…and {b.count - b.employees.length} more</li>}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-slate-50 px-3 py-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="text-base font-semibold tabular-nums text-slate-900">{value}</p>
    </div>
  );
}

/** A payroll run started from the chat: a LangGraph workflow that waits for HR's decisions. */
export function PayrollFlowCard({ card, token, onNavigate }: {
  card: Record<string, unknown>; token: string; onNavigate?: (page: PageId) => void;
}) {
  const [flow, setFlow] = useState<Flow>(card as unknown as Flow);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // a card from the chat history may be old: load the current step
  useEffect(() => {
    getPayrollFlow(token, flow.flow_id).then(setFlow).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per card
  }, [token]);

  const decide = useCallback(async (decision: FlowDecision) => {
    setBusy(true);
    setError(null);
    try {
      setFlow(await resumePayrollFlow(token, flow.flow_id, decision));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      throw err;
    } finally {
      setBusy(false);
    }
  }, [token, flow.flow_id]);

  const onFinished = useCallback(() => { void decide({ action: "continue" }).catch(() => undefined); }, [decide]);

  return (
    <div className="w-full max-w-2xl space-y-3 overflow-hidden rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-indigo-700">Payroll run · {HEADLINE[flow.stage]}</p>
          <h3 className="text-base font-semibold text-slate-900">{flow.label} · {flow.scope}</h3>
        </div>
        {flow.started_by && <span className="text-[11px] text-slate-500">started by {flow.started_by}</span>}
      </div>
      <Stepper flow={flow} />

      {flow.stage === "approval" && (
        <>
          <Prechecks flow={flow} />
          <Blockers flow={flow} />
          {flow.batch_id && !flow.blockers.length && (
            <BulkLopCard batchId={flow.batch_id} token={token}
              onRun={(mode, reason) => decide({ action: "approve", mode, reason })} />
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" disabled={busy} onClick={() => void decide({ action: "recheck" }).catch(() => undefined)}
              className="flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              <RefreshCw className="h-3.5 w-3.5" /> Re-check
            </button>
            <button type="button" disabled={busy} onClick={() => void decide({ action: "cancel" }).catch(() => undefined)}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              Cancel run
            </button>
          </div>
        </>
      )}

      {flow.stage === "running" && flow.batch_id && (
        <BulkLopCard batchId={flow.batch_id} token={token} onFinished={onFinished} />
      )}

      {flow.stage === "done" && (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Tile label="Employees paid" value={String(flow.totals?.employees ?? 0)} />
            <Tile label="Total gross" value={formatINR(flow.totals?.gross ?? "0")} />
            <Tile label="Total net pay" value={formatINR(flow.totals?.net ?? "0")} />
          </div>
          {flow.run && flow.run.failed > 0 && (
            <p className="flex items-center gap-1.5 text-xs text-amber-700"><TriangleAlert className="h-3.5 w-3.5" />
              {flow.run.failed} employee(s) failed - see Payroll Runs to retry them.</p>
          )}
          {flow.validation && (
            <p className={`text-sm ${flow.validation.errors ? "text-red-700" : "text-emerald-700"}`}>
              Validation: {flow.validation.errors} error(s), {flow.validation.warnings} warning(s), {flow.validation.passed} passed.
              {flow.validation.note ? ` ${flow.validation.note}` : ""}
            </p>
          )}
          <p className="text-xs text-slate-600">
            Next: a second person approves and finalizes (locks) the payroll on the Payroll Runs page.
          </p>
          {onNavigate && (
            <button type="button" onClick={() => onNavigate("payroll-runs")}
              className="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800">
              Open Payroll Runs <ArrowRight className="h-3.5 w-3.5" />
            </button>
          )}
        </>
      )}

      {flow.stage === "cancelled" && (
        <p className="text-sm text-slate-600">Run cancelled - nothing was processed.</p>
      )}

      {flow.stage === "error" && (
        <div className="flex items-center justify-between gap-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          <span>A step failed{flow.error ? `: ${flow.error}` : ""}.</span>
          <button type="button" disabled={busy} onClick={() => void decide({ action: "retry" }).catch(() => undefined)}
            className="rounded-lg bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50">Retry</button>
        </div>
      )}

      {(error || (flow.stage === "approval" && flow.error)) && (
        <p role="alert" className="text-sm text-red-600">{error ?? flow.error}</p>
      )}
    </div>
  );
}
