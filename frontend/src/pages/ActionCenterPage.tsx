import { useCan } from "../lib/permissions";
import { BadgeCheck, ChevronDown, ChevronUp, CircleX, Inbox, Lock, MessageSquare, MonitorSmartphone, TriangleAlert, Undo2, UserCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DiffTable } from "../components/InsightCards";
import { useToast } from "../components/layout/Toast";
import {
  ApiError, actionPreview, approvalSetting, approveAction, listActions, rejectAction, setApprovalSetting, undoAction, undoPreview,
  type ActionItem, type ActionPage, type ActionStatus, type ChangePreview,
} from "../lib/api";
import { formatINR } from "../lib/format";
import type { User } from "../lib/types";

// Display only: previews and results come from the backend; approving re-checks the numbers.

const TABS: { key: ActionStatus; label: string; tone: string }[] = [
  { key: "PENDING_APPROVAL", label: "Pending approval", tone: "text-amber-700" },
  { key: "APPLIED", label: "Completed", tone: "text-emerald-700" },
  { key: "REJECTED", label: "Rejected", tone: "text-rose-700" },
  { key: "FAILED", label: "Failed", tone: "text-rose-700" },
  { key: "CANCELLED", label: "Withdrawn", tone: "text-slate-500" },
];

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

function Impact({ value }: { value: string }) {
  const n = Number(value);
  return (
    <span className={`font-semibold tabular-nums ${n > 0 ? "text-emerald-700" : n < 0 ? "text-rose-700" : "text-slate-500"}`}>
      {n === 0 ? "No net change" : `${n > 0 ? "+" : "−"}${formatINR(String(Math.abs(n)))}`}
    </span>
  );
}

function Row({ item, me, approvers, onApprove, onReject, onUndo }: {
  item: ActionItem; me: User; approvers: string[]; onApprove: (i: ActionItem) => void; onReject: (i: ActionItem) => void;
  onUndo: (i: ActionItem) => void;
}) {
  const can = useCan();
  const [open, setOpen] = useState(false);
  const pending = item.status === "PENDING_APPROVAL";
  const mine = item.requested_by_code === me.employee_code;
  const undone = item.result?.undone_by;
  const undoable = item.status === "APPLIED" && !!item.result?.undo && !undone && (can("edit_attendance") || can("edit_pay"));
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-900">{item.employee_name} <span className="font-normal text-slate-400">{item.employee_code} · {item.period_label}</span></p>
          <p className="text-sm text-slate-700">{item.title}</p>
          <p className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-500">
            {item.source === "chat" ? <MessageSquare className="h-3 w-3" /> : <MonitorSmartphone className="h-3 w-3" />}
            {item.requested_by} · {when(item.requested_at)} · “{item.reason}”
          </p>
          {item.decided_by && <p className="text-[11px] text-slate-500">{item.status === "APPLIED" ? "Approved" : item.status === "CANCELLED" ? "Withdrawn" : "Decided"} by {item.decided_by} · {when(item.decided_at)}{item.decision_note ? ` · “${item.decision_note}”` : ""}</p>}
          {item.error && <p className="mt-1 flex gap-1 text-xs text-rose-700"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />{item.error}</p>}
          {item.result?.recalculated?.length ? <p className="text-[11px] text-emerald-700">Payslip recalculated: {item.result.recalculated.join(", ")}</p> : null}
          {undone && <p className="text-[11px] text-slate-500">Undone by action {undone}</p>}
          {item.source === "undo" && <p className="text-[11px] text-fuchsia-700">This change reversed an earlier one</p>}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs">Impact: <Impact value={item.net_change} /></span>
          <button type="button" onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50">
            Review {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          </button>
          {pending && !mine && can("approve") && (
            <button type="button" onClick={() => onApprove(item)} className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700">
              <BadgeCheck className="h-3.5 w-3.5" />Approve
            </button>
          )}
          {pending && mine && (
            <button type="button" disabled title="You requested this change - a different HR user must approve it (maker-checker)"
              className="inline-flex cursor-not-allowed items-center gap-1 rounded-lg bg-slate-200 px-2.5 py-1 text-xs font-medium text-slate-500">
              <Lock className="h-3.5 w-3.5" />Approve
            </button>
          )}
          {undoable && (
            <button type="button" onClick={() => onUndo(item)} className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50">
              <Undo2 className="h-3.5 w-3.5" />Undo
            </button>
          )}
          {item.status === "APPLIED" && !item.result?.undo && !undone && item.result?.undo_note && (
            <span title={item.result.undo_note} className="inline-flex cursor-help items-center gap-1 rounded-lg bg-slate-100 px-2.5 py-1 text-xs text-slate-400">
              <Undo2 className="h-3.5 w-3.5" />Not undoable
            </span>
          )}
          {pending && (mine || can("approve")) && (
            <button type="button" onClick={() => onReject(item)} className="inline-flex items-center gap-1 rounded-lg border border-rose-200 px-2.5 py-1 text-xs font-medium text-rose-700 hover:bg-rose-50">
              <CircleX className="h-3.5 w-3.5" />{mine ? "Withdraw" : "Reject"}
            </button>
          )}
        </div>
      </div>
      {pending && mine && (
        <p className="mt-1.5 flex items-center gap-1.5 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-800">
          <UserCheck className="h-3.5 w-3.5 shrink-0" />
          You requested this, so you can't approve it yourself (maker-checker).
          {approvers.length ? ` Ask ${approvers.join(" or ")} to log in and approve it` : " Another HR user must approve it"}, or switch approval OFF above.
        </p>
      )}
      {open && <div className="mt-3 rounded-xl border border-slate-200 p-3"><p className="mb-1 text-[11px] text-slate-400">Numbers the requester confirmed</p><DiffTable rows={item.preview.rows} /></div>}
    </li>
  );
}

export function ActionCenterPage({ token, user, onExpired, onChanged }: {
  token: string; user: User; onExpired: () => void; onChanged?: () => void;
}) {
  const toast = useToast();
  const can = useCan();
  const [tab, setTab] = useState<ActionStatus>("PENDING_APPROVAL");
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [data, setData] = useState<ActionPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approving, setApproving] = useState<{ item: ActionItem; fresh: ChangePreview | null; error?: string } | null>(null);
  const [rejecting, setRejecting] = useState<ActionItem | null>(null);
  const [undoing, setUndoing] = useState<{ item: ActionItem; fresh: ChangePreview | null; error?: string } | null>(null);
  const [approvalOn, setApprovalOn] = useState<boolean | null>(null);
  const [approvers, setApprovers] = useState<{ name: string; email: string }[]>([]);
  const [toggling, setToggling] = useState(false);

  useEffect(() => {
    approvalSetting(token).then((r) => { setApprovalOn(r.change_approval); setApprovers(r.approvers); }).catch(() => undefined);
  }, [token]);

  const load = useCallback(() => listActions(token, tab, page, search).then(setData).catch((e) => {
    if (e instanceof ApiError && e.status === 401) return onExpired();
    setError(e instanceof ApiError ? e.message : "Could not load actions");
  }), [token, tab, page, search, onExpired]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setPage(1); }, [tab, search]);

  async function openApprove(item: ActionItem) {
    setApproving({ item, fresh: null });
    try {
      setApproving({ item, fresh: await actionPreview(token, item.id) });
    } catch (e) {
      setApproving({ item, fresh: null, error: e instanceof ApiError ? e.message : "Could not calculate the preview" });
    }
  }

  async function openUndo(item: ActionItem) {
    setUndoing({ item, fresh: null });
    try {
      setUndoing({ item, fresh: await undoPreview(token, item.id) });
    } catch (e) {
      setUndoing({ item, fresh: null, error: e instanceof ApiError ? e.message : "Could not calculate the undo" });
    }
  }

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white px-5 py-3 shadow-sm">
        <div>
          <p className="text-sm font-semibold text-slate-900">Approval of payroll changes</p>
          <p className="text-xs text-slate-500">
            {approvalOn == null ? "…" : approvalOn
              ? "ON — LOP, attendance, overtime, one-off pay and salary changes wait for a second HR user."
              : "OFF — confirmed changes are applied at once (still recorded here as Completed)."}
          </p>
        </div>
        {approvalOn != null && can("admin") && (
          <button type="button" role="switch" aria-checked={approvalOn} onClick={() => setToggling(true)}
            className={`relative h-6 w-11 rounded-full transition ${approvalOn ? "bg-indigo-600" : "bg-slate-300"}`}>
            <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition ${approvalOn ? "left-5" : "left-0.5"}`} />
          </button>
        )}
      </section>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {TABS.map((t) => (
          <button key={t.key} type="button" onClick={() => setTab(t.key)}
            className={`rounded-2xl border bg-white p-4 text-left shadow-sm transition ${tab === t.key ? "border-indigo-400 ring-2 ring-indigo-100" : "border-slate-200 hover:border-slate-300"}`}>
            <p className="text-xs font-medium text-slate-500">{t.label}</p>
            <p className={`mt-1 text-2xl font-semibold ${t.tone}`}>{data ? data.counts[t.key] : "–"}</p>
          </button>
        ))}
      </div>

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Inbox className="h-4 w-4 text-slate-400" />{TABS.find((t) => t.key === tab)!.label}</p>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search employee" maxLength={100}
            className="w-52 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
        </div>
        {!data ? <div className="mt-4 h-32 animate-pulse rounded-xl bg-slate-100" /> : data.items.length === 0 ? (
          <div className="py-10 text-center">
            <Inbox className="mx-auto h-8 w-8 text-slate-300" />
            <p className="mt-2 text-sm text-slate-500">Nothing here.</p>
            {tab === "PENDING_APPROVAL" && <p className="text-xs text-slate-400">Changes from Employees or AI Chat that need approval appear here.</p>}
          </div>
        ) : (
          <>
            {tab === "PENDING_APPROVAL" && data.items.some((i) => i.requested_by_code === user.employee_code) && (
              <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
                <p className="font-semibold">Some of these requests are yours — you can't approve your own changes.</p>
                <p className="mt-1">
                  {approvers.length
                    ? <>Approver{approvers.length > 1 ? "s" : ""}: {approvers.map((a) => `${a.name} (${a.email})`).join(", ")}. Log out and sign in as one of them to approve, </>
                    : <>No other HR user can approve yet. </>}
                  or switch “Approval of payroll changes” OFF above so your changes apply immediately.
                </p>
              </div>
            )}
            <ul className="mt-2 divide-y divide-slate-100">
              {data.items.map((i) => <Row key={i.id} item={i} me={user} approvers={approvers.map((a) => a.name)} onApprove={openApprove} onReject={setRejecting} onUndo={openUndo} />)}
            </ul>
          </>
        )}
        {data && data.pages > 1 && (
          <div className="flex items-center justify-between pt-2 text-xs text-slate-600">
            <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Prev</button>
            <span>Page {data.page} of {data.pages}</span>
            <button type="button" disabled={page >= data.pages} onClick={() => setPage(page + 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next ›</button>
          </div>
        )}
      </section>

      {approving && (
        <ConfirmDialog title={`Approve · ${approving.item.title}`} confirmLabel="Approve & Apply" tone="emerald"
          facts={[{ label: "Employee", value: `${approving.item.employee_name} (${approving.item.employee_code})` },
            { label: "Month", value: approving.item.period_label }, { label: "Requested by", value: approving.item.requested_by ?? "—" },
            { label: "Reason", value: approving.item.reason }]}
          warning={approving.fresh && approving.fresh.net_change !== approving.item.net_change
            ? `Payroll data changed since the request: the net impact is now ${formatINR(approving.fresh.net_change)} (was ${formatINR(approving.item.net_change)}). You are approving today's numbers below.`
            : undefined}
          onClose={() => setApproving(null)}
          onConfirm={async () => {
            if (!approving.fresh) throw new ApiError(0, approving.error ?? "Preview not ready yet");
            const r = await approveAction(token, approving.item.id, approving.fresh.preview_id);
            toast(r.status === "APPLIED" ? `Approved and applied for ${r.employee_name}` : (r.message ?? `Not applied: ${r.error}`), r.status === "APPLIED" ? "success" : "error");
            await load();
            onChanged?.();
          }}>
          {approving.error ? <p className="text-sm text-rose-700">{approving.error}</p>
            : !approving.fresh ? <div className="h-24 animate-pulse rounded-lg bg-slate-100" />
              : <div><p className="mb-1 text-[11px] text-slate-400">Today's preview (what will be applied)</p><DiffTable rows={approving.fresh.rows} /></div>}
        </ConfirmDialog>
      )}
      {toggling && approvalOn != null && (
        <ConfirmDialog title={approvalOn ? "Turn approval OFF" : "Turn approval ON"} confirmLabel={approvalOn ? "Turn off" : "Turn on"}
          tone={approvalOn ? "rose" : "indigo"} reasonLabel="Reason (required, audited)"
          facts={[{ label: "Applies to", value: "LOP, attendance, overtime, one-off pay, salary changes" },
            { label: "From", value: "now" }]}
          warning={approvalOn ? "Without approval, one HR user can change payroll alone. Pending requests stay pending until decided." : undefined}
          onClose={() => setToggling(false)}
          onConfirm={async (reason) => {
            const r = await setApprovalSetting(token, !approvalOn, reason);
            setApprovalOn(r.change_approval);
            setApprovers(r.approvers);
            toast(`Approval of payroll changes is now ${r.change_approval ? "ON" : "OFF"}`, "success");
          }} />
      )}
      {undoing && (
        <ConfirmDialog title={`Undo · ${undoing.item.title}`} confirmLabel={undoing.fresh?.requires_approval ? "Submit undo for approval" : "Undo change"} tone="rose"
          reasonLabel="Why undo? (required, audited)"
          facts={[{ label: "Employee", value: `${undoing.item.employee_name} (${undoing.item.employee_code})` },
            { label: "Month", value: undoing.item.period_label }, { label: "Original reason", value: undoing.item.reason }]}
          warning="Undo is a new change: it restores the earlier values and recalculates the payslip. The original stays in the history."
          onClose={() => setUndoing(null)}
          onConfirm={async (reason) => {
            if (!undoing.fresh) throw new ApiError(0, undoing.error ?? "Preview not ready yet");
            const r = await undoAction(token, undoing.item.id, undoing.fresh.preview_id, reason);
            toast(r.status === "APPLIED" ? `Undone for ${r.employee_name}` : r.status === "PENDING_APPROVAL" ? "Undo sent for approval" : (r.message ?? `Not applied: ${r.error}`),
              r.status === "FAILED" ? "error" : "success");
            await load();
            onChanged?.();
          }}>
          {undoing.error ? <p className="text-sm text-rose-700">{undoing.error}</p>
            : !undoing.fresh ? <div className="h-24 animate-pulse rounded-lg bg-slate-100" />
              : <div><p className="mb-1 text-[11px] text-slate-400">After the undo · net impact {formatINR(undoing.fresh.net_change)}</p><DiffTable rows={undoing.fresh.rows} /></div>}
        </ConfirmDialog>
      )}
      {rejecting && (
        <ConfirmDialog title={rejecting.requested_by_code === user.employee_code ? "Withdraw request" : "Reject request"}
          confirmLabel={rejecting.requested_by_code === user.employee_code ? "Withdraw" : "Reject"} tone="rose" reasonLabel="Note (required)"
          facts={[{ label: "Employee", value: rejecting.employee_name }, { label: "Change", value: rejecting.title }, { label: "Month", value: rejecting.period_label }]}
          onClose={() => setRejecting(null)}
          onConfirm={async (note) => { await rejectAction(token, rejecting.id, note); toast("Request closed - nothing was applied", "info"); await load(); onChanged?.(); }} />
      )}
    </div>
  );
}
