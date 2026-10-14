import { useState } from "react";
import {
  ApiError, actionPreview, approveAction, cancelLeave, decideLeave, rejectAction,
  type ActionItem, type ChangePreview, type LeaveRow,
} from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import { ConfirmDialog } from "./ConfirmDialog";
import { DiffTable } from "./InsightCards";

// Chat cards for decisions: leave requests (approve / reject / withdraw) and payroll changes
// waiting for approval (maker-checker). Every button opens a confirm dialog; the backend
// checks the role, the maker-checker rule and the request's status again.

const shortDate = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

const STATUS_TONE: Record<string, string> = {
  PENDING: "bg-amber-50 text-amber-800", PENDING_APPROVAL: "bg-amber-50 text-amber-800",
  APPROVED: "bg-emerald-50 text-emerald-700", APPLIED: "bg-emerald-50 text-emerald-700",
  REJECTED: "bg-rose-50 text-rose-700", FAILED: "bg-rose-50 text-rose-700",
  CANCELLED: "bg-slate-100 text-slate-500",
};

function Badge({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_TONE[status] ?? "bg-slate-100 text-slate-600"}`}>
      {status === "CANCELLED" ? "withdrawn" : status.replace("_", " ").toLowerCase()}
    </span>
  );
}

function Shell({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-100 px-4 py-3">
        <p className="text-xs font-medium uppercase tracking-wide text-indigo-700">{subtitle}</p>
        <h3 className="text-base font-semibold text-slate-900">{title}</h3>
      </div>
      <ul className="divide-y divide-slate-100">{children}</ul>
    </div>
  );
}

const button = "rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50";

export function LeaveListCard({ card, token }: { card: Record<string, unknown>; token: string }) {
  const approving = card.mode === "approve";
  const [items, setItems] = useState<LeaveRow[]>((card.items as LeaveRow[]) ?? []);
  const [open, setOpen] = useState<{ row: LeaveRow; kind: "approve" | "reject" | "cancel" } | null>(null);

  const replace = (row: LeaveRow) => setItems((all) => all.map((r) => (r.id === row.id ? { ...r, ...row } : r)));

  return (
    <Shell title={approving ? "Leave requests to decide" : "My leave requests"} subtitle="Leave">
      {items.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm">
          <div>
            <p className="font-medium text-slate-900">
              {approving && <>{r.employee_name} <span className="text-xs font-normal text-slate-400">{r.employee_code}</span> · </>}
              {r.leave_label}
            </p>
            <p className="text-xs text-slate-500">
              {shortDate(r.from)}{r.to !== r.from && ` – ${shortDate(r.to)}`} · {formatDays(r.days)} day(s){r.reason ? ` · ${r.reason}` : ""}
            </p>
          </div>
          {r.status !== "PENDING" ? <Badge status={r.status} /> : approving ? (
            <div className="flex gap-1.5">
              <button type="button" onClick={() => setOpen({ row: r, kind: "reject" })} className={`${button} text-rose-700 hover:bg-rose-50`}>Reject</button>
              <button type="button" onClick={() => setOpen({ row: r, kind: "approve" })} className={`${button} bg-emerald-600 text-white hover:bg-emerald-700`}>Approve</button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <Badge status={r.status} />
              <button type="button" onClick={() => setOpen({ row: r, kind: "cancel" })} className={`${button} text-rose-700 hover:bg-rose-50`}>Cancel</button>
            </div>
          )}
        </li>
      ))}
      {open && (
        <ConfirmDialog
          title={{ approve: "Approve leave", reject: "Reject leave", cancel: "Withdraw leave request" }[open.kind]}
          confirmLabel={{ approve: "Approve", reject: "Reject", cancel: "Withdraw" }[open.kind]}
          tone={open.kind === "approve" ? "emerald" : "rose"}
          reasonLabel={open.kind === "reject" ? "Note for the employee (required)" : undefined}
          facts={[...(approving ? [{ label: "Employee", value: `${open.row.employee_name} (${open.row.employee_code})` }] : []),
            { label: "Leave", value: open.row.leave_label },
            { label: "Dates", value: `${shortDate(open.row.from)} – ${shortDate(open.row.to)} · ${formatDays(open.row.days)} day(s)` }]}
          warning={open.kind === "approve" ? "Approving writes the leave into attendance and recalculates a processed payslip of that month." : undefined}
          onClose={() => setOpen(null)}
          onConfirm={async (note) => {
            replace(open.kind === "cancel" ? await cancelLeave(token, open.row.id)
              : await decideLeave(token, open.row.id, open.kind === "approve", note));
          }} />
      )}
    </Shell>
  );
}

export function ActionListCard({ card, token }: { card: Record<string, unknown>; token: string }) {
  const me = String(card.me);
  const canApprove = Boolean(card.can_approve);
  const total = Number(card.total);
  const [items, setItems] = useState<ActionItem[]>((card.items as ActionItem[]) ?? []);
  const [approving, setApproving] = useState<{ item: ActionItem; fresh: ChangePreview | null; error?: string } | null>(null);
  const [rejecting, setRejecting] = useState<ActionItem | null>(null);

  const replace = (row: ActionItem) => setItems((all) => all.map((i) => (i.id === row.id ? row : i)));

  async function openApprove(item: ActionItem) {
    setApproving({ item, fresh: null });
    try {
      setApproving({ item, fresh: await actionPreview(token, item.id) });
    } catch (e) {
      setApproving({ item, fresh: null, error: e instanceof ApiError ? e.message : "Could not calculate the preview" });
    }
  }

  return (
    <Shell title={`Payroll changes waiting for approval${total > items.length ? ` · first ${items.length} of ${total}` : ""}`} subtitle="Approvals">
      {items.map((i) => {
        const mine = i.requested_by_code === me;
        return (
          <li key={i.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm">
            <div>
              <p className="font-medium text-slate-900">{i.title}</p>
              <p className="text-xs text-slate-500">
                {i.employee_name} ({i.employee_code}) · {i.period_label} · net {formatINR(i.net_change)} · by {mine ? "you" : i.requested_by}
              </p>
            </div>
            {i.status !== "PENDING_APPROVAL" ? <Badge status={i.status} /> : (
              <div className="flex gap-1.5">
                <button type="button" onClick={() => setRejecting(i)} className={`${button} text-rose-700 hover:bg-rose-50`}>{mine ? "Withdraw" : "Reject"}</button>
                {!mine && canApprove && (
                  <button type="button" onClick={() => openApprove(i)} className={`${button} bg-emerald-600 text-white hover:bg-emerald-700`}>Approve</button>
                )}
              </div>
            )}
          </li>
        );
      })}
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
            if (r.status !== "APPLIED") throw new ApiError(0, r.message ?? `Not applied: ${r.error}`);
            replace(r);
          }}>
          {approving.error ? <p className="text-sm text-rose-700">{approving.error}</p>
            : !approving.fresh ? <div className="h-24 animate-pulse rounded-lg bg-slate-100" />
              : <div><p className="mb-1 text-[11px] text-slate-400">Today's preview (what will be applied)</p><DiffTable rows={approving.fresh.rows} /></div>}
        </ConfirmDialog>
      )}
      {rejecting && (
        <ConfirmDialog title={rejecting.requested_by_code === me ? "Withdraw request" : "Reject request"}
          confirmLabel={rejecting.requested_by_code === me ? "Withdraw" : "Reject"} tone="rose" reasonLabel="Note (required)"
          facts={[{ label: "Employee", value: rejecting.employee_name }, { label: "Change", value: rejecting.title }, { label: "Month", value: rejecting.period_label }]}
          onClose={() => setRejecting(null)}
          onConfirm={async (note) => { replace(await rejectAction(token, rejecting.id, note)); }} />
      )}
    </Shell>
  );
}
