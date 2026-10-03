import { ShieldAlert, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "../lib/api";

/** Every payroll write goes through this: facts first, then an explicit confirm.
 *  Nothing is sent until the user presses the confirm button. */
export function ConfirmDialog({
  title, facts, warning, confirmLabel, tone = "indigo", reasonLabel, onConfirm, onClose, children,
}: {
  title: string;
  facts: { label: string; value: ReactNode }[];
  warning?: string;
  confirmLabel: string;
  tone?: "indigo" | "rose" | "emerald";
  /** when set, a reason (min 5 characters) is required and passed to onConfirm */
  reasonLabel?: string;
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
  children?: ReactNode;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsReason = Boolean(reasonLabel);
  const ready = !needsReason || reason.trim().length >= 5;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason.trim());
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  const button = {
    indigo: "bg-indigo-600 hover:bg-indigo-700", rose: "bg-rose-600 hover:bg-rose-700",
    emerald: "bg-emerald-600 hover:bg-emerald-700",
  }[tone];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => !busy && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md overflow-hidden rounded-2xl bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-amber-50 text-amber-600"><ShieldAlert className="h-5 w-5" /></span>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">Review &amp; confirm</p>
              <h2 className="text-base font-semibold text-slate-900">{title}</h2>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className="rounded-lg p-1 text-slate-400 hover:bg-slate-100"><X className="h-4 w-4" /></button>
        </div>
        <div className="space-y-4 px-5 py-4">
          <dl className="divide-y divide-slate-100 rounded-xl border border-slate-200 text-sm">
            {facts.map((f) => (
              <div key={f.label} className="flex justify-between gap-4 px-3 py-2">
                <dt className="text-slate-500">{f.label}</dt><dd className="text-right font-medium text-slate-900">{f.value}</dd>
              </div>
            ))}
          </dl>
          {children}
          {warning && <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">{warning}</p>}
          {needsReason && (
            <label className="block text-xs font-medium text-slate-600">
              {reasonLabel}
              <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={500}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
            </label>
          )}
          {error && <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>}
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-3">
          <button type="button" onClick={onClose} disabled={busy}
            className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-200">Cancel</button>
          <button type="button" onClick={confirm} disabled={busy || !ready}
            className={`rounded-lg px-4 py-2 text-sm font-medium text-white disabled:opacity-50 ${button}`}>
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
