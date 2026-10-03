import { CalendarPlus, CheckCircle2, Landmark, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, applyLeave, previewLeave, taxCompare, type LeaveForm, type LeavePlan } from "../lib/api";
import { formatINR } from "../lib/format";
import { Shell } from "./InsightCards";

// Leave application and tax-regime cards in AI Chat. Display only: the backend checks
// the leave (days, balance, overlaps) and calculates every tax figure.

/* eslint-disable @typescript-eslint/no-explicit-any -- card payloads are backend JSON */
type Card = Record<string, any>;

export const LEAVE_TYPES = [
  { value: "CL", label: "Casual leave" }, { value: "SL", label: "Sick leave" },
  { value: "EL", label: "Earned leave" }, { value: "LOP", label: "Leave without pay" },
];
const field = "mt-0.5 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-xs outline-none focus:border-indigo-500";
const fmtDate = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });

/** The leave form + live check; used by the chat card and the Leave page. */
export function LeaveApplyForm({ token, initial, initialPlan, initialError, onApplied }: {
  token: string; initial: LeaveForm; initialPlan?: LeavePlan | null; initialError?: string | null;
  onApplied: (approvers: string[]) => void;
}) {
  const [form, setForm] = useState<LeaveForm>(initial);
  const [plan, setPlan] = useState<LeavePlan | null>(initialPlan ?? null);
  const [error, setError] = useState<string | null>(initialError ?? null);
  const [busy, setBusy] = useState(false);
  const first = useRef(!!(initialPlan || initialError));

  useEffect(() => {
    if (first.current) { first.current = false; return; }
    if (!form.from) { setPlan(null); setError("Pick the dates."); return; }
    let live = true;
    const t = setTimeout(() => {
      previewLeave(token, form)
        .then((p) => { if (live) { setPlan(p); setError(null); } })
        .catch((e) => { if (live) { setPlan(null); setError(e instanceof ApiError ? e.message : "Could not check the leave"); } });
    }, 300);
    return () => { live = false; clearTimeout(t); };
    // the reason does not change the check
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, form.leave_type, form.from, form.to]);

  async function submit() {
    setBusy(true);
    try {
      const r = await applyLeave(token, form);
      onApplied(r.approvers);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not apply");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <label className="text-[11px] font-medium text-slate-600">Type
          <select value={form.leave_type} onChange={(e) => setForm({ ...form, leave_type: e.target.value })} className={field}>
            {LEAVE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </label>
        <label className="text-[11px] font-medium text-slate-600">From
          <input type="date" value={form.from} onChange={(e) => setForm({ ...form, from: e.target.value, to: form.to && form.to >= e.target.value ? form.to : e.target.value })} className={field} />
        </label>
        <label className="text-[11px] font-medium text-slate-600">To
          <input type="date" value={form.to} min={form.from} onChange={(e) => setForm({ ...form, to: e.target.value })} className={field} />
        </label>
      </div>
      <label className="block text-[11px] font-medium text-slate-600">Reason
        <input value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} maxLength={500} placeholder="e.g. Family function" className={field} />
      </label>
      {error && <p className="flex gap-1.5 rounded-lg bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />{error}</p>}
      {plan && (
        <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-700">
          <p><span className="font-semibold">{plan.days} working day(s)</span> · {plan.from === plan.to ? fmtDate(plan.from) : `${fmtDate(plan.from)} – ${fmtDate(plan.to)}`}</p>
          {plan.balance && <p>{plan.leave_label} balance: {plan.balance.available} → <span className="font-semibold">{plan.balance.after}</span></p>}
          {plan.skipped.length > 0 && <p className="text-slate-500">Not counted: {plan.skipped.map((d) => `${fmtDate(d.date)} (${d.why})`).join(", ")}</p>}
          {plan.warnings.map((w) => <p key={w} className="text-amber-700">{w}</p>)}
          <p className="text-slate-500">Approver: {plan.approvers.slice(0, 3).join(", ") || "HR"}</p>
        </div>
      )}
      <button type="button" disabled={!plan || busy || form.reason.trim().length < 3} onClick={submit}
        className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
        <CalendarPlus className="h-3.5 w-3.5" />{busy ? "Applying…" : "Apply for leave"}
      </button>
      {plan && form.reason.trim().length < 3 && <span className="ml-2 text-[11px] text-slate-400">Add a reason to apply</span>}
    </div>
  );
}

export function LeaveRequestCard({ card, token }: { card: Card; token: string }) {
  const [sent, setSent] = useState<string[] | null>(null);
  return (
    <Shell icon={<CalendarPlus className="h-4 w-4" />} title="Leave application" subtitle="Checked against your balance - sent only when you press Apply">
      {sent ? (
        <p className="flex items-center gap-1.5 text-sm text-emerald-700"><CheckCircle2 className="h-4 w-4" />
          Applied. Waiting for approval from {sent.slice(0, 2).join(" or ") || "HR"}. Track it on the Leave page.</p>
      ) : (
        <>
          {card.type_guessed && card.plan && <p className="mb-2 text-[11px] text-slate-500">No leave type mentioned - Casual leave selected; change it below if needed.</p>}
          <LeaveApplyForm token={token} initial={{ leave_type: card.form.leave_type, from: card.form.from ?? "", to: card.form.to ?? "", reason: card.form.reason ?? "" }}
            initialPlan={card.plan} initialError={card.error} onApplied={setSent} />
        </>
      )}
    </Shell>
  );
}

const SECTIONS = [
  { key: "80C", label: "80C (PF, PPF, ELSS, LIC…)" }, { key: "80D", label: "80D (health insurance)" },
  { key: "80CCD(1B)", label: "80CCD(1B) (NPS)" }, { key: "80TTA", label: "80TTA (savings interest)" },
];

export function TaxCompareCard({ card: initial, token }: { card: Card; token: string }) {
  const [card, setCard] = useState<Card>(initial);
  const [declared, setDeclared] = useState<Record<string, string>>(
    Object.fromEntries(SECTIONS.map((s) => [s.key, String(initial.declared?.[s.key] ?? "")])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [old, neu] = card.regimes as Card[];
  const rows: [string, string][] = [
    ["Projected salary (FY)", "gross"], ["Standard deduction", "standard_deduction"], ["Professional Tax", "professional_tax"],
    ["Taxable income", "taxable_income"], ["Rebate", "rebate"], ["Cess", "cess"], ["Total tax", "total_tax"], ["Average per month", "monthly_average"],
  ];
  const deductions = (r: Card) => Object.values(r.deductions ?? {}).reduce((a: number, v) => a + Number(v), 0);

  async function recalc() {
    setBusy(true);
    setError(null);
    try {
      setCard(await taxCompare(token, declared, initial.employee_code));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not recalculate");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell icon={<Landmark className="h-4 w-4" />} title={`Old vs new tax regime · FY ${card.financial_year}`} subtitle={`${card.employee_name} · current regime ${card.current_regime.toLowerCase()} · based on ${card.period_label}`}>
      {!(old.available && neu.available) ? <p className="text-xs text-rose-700">{(old.available ? neu : old).reason}</p> : (
        <>
          <table className="w-full text-xs">
            <thead className="text-[11px] uppercase text-slate-500"><tr><th className="py-1 text-left" />
              {[old, neu].map((r) => <th key={r.regime} className={`py-1 text-right ${card.better === r.regime ? "text-emerald-700" : ""}`}>{r.regime === "OLD" ? "Old" : "New"} regime{card.better === r.regime ? " ✓" : ""}</th>)}</tr></thead>
            <tbody className="divide-y divide-slate-100">
              {rows.slice(0, 3).map(([label, k]) => <tr key={k}><td className="py-1 text-slate-600">{label}</td>{[old, neu].map((r) => <td key={r.regime} className="text-right tabular-nums">{formatINR(r[k])}</td>)}</tr>)}
              <tr><td className="py-1 text-slate-600">Deductions (80C, 80D…)</td>{[old, neu].map((r) => <td key={r.regime} className="text-right tabular-nums">{formatINR(deductions(r))}</td>)}</tr>
              {rows.slice(3).map(([label, k]) => <tr key={k} className={k === "total_tax" ? "font-semibold text-slate-900" : ""}><td className="py-1 text-slate-600">{label}</td>{[old, neu].map((r) => <td key={r.regime} className="text-right tabular-nums">{formatINR(r[k])}</td>)}</tr>)}
            </tbody>
          </table>
          <p className="mt-2 rounded-lg bg-emerald-50 px-2.5 py-1.5 text-xs text-emerald-800">
            {card.better === "SAME" ? "Both regimes give the same tax." : `The ${card.better.toLowerCase()} regime is lower by ${formatINR(card.saving)} for the year.`}
          </p>
          {card.tips?.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-[11px] text-slate-600">
              {card.tips.map((t: Card) => <li key={t.section}>Unused {t.section} limit {formatINR(t.unused)} → old-regime tax down by {formatINR(t.tax_saved)}{t.beats_new ? " (would beat the new regime)" : ""}</li>)}
            </ul>
          )}
        </>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-xs font-medium text-indigo-700">Try your own deductions (old regime)</summary>
        <div className="mt-2 grid grid-cols-2 gap-2">
          {SECTIONS.map((s) => (
            <label key={s.key} className="text-[11px] text-slate-600">{s.label}
              <input inputMode="numeric" value={declared[s.key]} onChange={(e) => setDeclared({ ...declared, [s.key]: e.target.value.replace(/[^\d]/g, "") })} placeholder="0" className={field} />
            </label>
          ))}
        </div>
        <button type="button" disabled={busy} onClick={recalc} className="mt-2 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">{busy ? "Calculating…" : "Recalculate"}</button>
        {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}
      </details>
      <p className="mt-2 text-[10px] text-slate-400">{card.disclaimer}</p>
    </Shell>
  );
}
