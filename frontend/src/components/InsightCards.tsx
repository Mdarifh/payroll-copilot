import { ArrowRight, BookOpen, Calculator, GitCompareArrows, Info, Scale, ShieldCheck, TriangleAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { ChangeDialog, kindOf } from "./ChangeDialog";
import { formatDays, formatINR } from "../lib/format";
import type { PageId } from "../lib/nav";

// Chat cards for routed answers. Display only: every value comes from the backend.

/* eslint-disable @typescript-eslint/no-explicit-any -- card payloads are backend JSON */
type Card = Record<string, any>;

export function Shell({ icon, title, subtitle, children }: { icon: ReactNode; title: string; subtitle?: string; children: ReactNode }) {
  return (
    <div className="w-full max-w-[85%] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 border-b border-slate-100 px-4 py-2.5">
        <span className="text-indigo-600">{icon}</span>
        <div>
          <p className="text-sm font-semibold text-slate-900">{title}</p>
          {subtitle && <p className="text-[11px] text-slate-500">{subtitle}</p>}
        </div>
      </div>
      <div className="px-4 py-3 text-sm">{children}</div>
    </div>
  );
}

const money = (v: string, unit?: string) => (unit === "DAYS" ? formatDays(v) : formatINR(v));
const signed = (v: string, unit?: string) => {
  const n = Number(v);
  if (n === 0) return "—";
  return `${n > 0 ? "+" : "−"}${money(String(Math.abs(n)), unit)}`;
};

function Steps({ steps }: { steps: { label: string; formula: string | null; result: string | null }[] }) {
  if (!steps?.length) return null;
  return (
    <ol className="mt-2 space-y-1.5 text-xs">
      {steps.map((s, i) => (
        <li key={i} className="flex justify-between gap-3">
          <span className="text-slate-600">{s.label}{s.formula && <span className="block text-[11px] text-slate-400">{s.formula}</span>}</span>
          {s.result != null && <span className="shrink-0 font-medium tabular-nums text-slate-900">{s.result}</span>}
        </li>
      ))}
    </ol>
  );
}

export function ExplainCard({ card }: { card: Card }) {
  const unitDays = card.component === "LOP";
  return (
    <Shell icon={<Calculator className="h-4 w-4" />} title={`${card.component} · ${card.employee_name}`} subtitle={card.month_label}>
      <p className="text-2xl font-semibold text-slate-900">{unitDays ? `${formatDays(card.amount)} days` : formatINR(card.amount)}</p>
      {card.applicable === false && card.reason && <p className="mt-1 text-xs text-slate-500">{card.reason}</p>}
      {card.formula && <p className="mt-1 text-[11px] text-slate-400">{card.formula}</p>}
      {card.segments?.length > 0 && (
        <table className="mt-3 w-full text-xs">
          <thead className="text-left text-[10px] uppercase text-slate-400">
            <tr><th className="py-1">Period</th><th>Rule</th><th className="text-right">Ceiling</th><th className="text-right">PF wage</th><th className="text-right">Employee PF</th></tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {card.segments.map((s: Card) => (
              <tr key={s.start}>
                <td className="py-1.5">{s.start.slice(8)}–{s.end.slice(8)}</td><td>{s.rule_version}</td>
                <td className="text-right tabular-nums">{formatINR(s.pf_ceiling)}</td>
                <td className="text-right tabular-nums">{formatINR(s.pf_wage)}</td>
                <td className="text-right tabular-nums">{formatINR(s.employee_pf)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!card.segments?.length && <Steps steps={card.steps} />}
      {card.earnings && (
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          {Object.entries(card.earnings as Record<string, string>).map(([k, v]) => (
            <div key={k} className="flex justify-between"><dt className="text-slate-500">{k}</dt><dd className="tabular-nums">{formatINR(v)}</dd></div>
          ))}
          {Object.entries((card.deductions ?? {}) as Record<string, string>).map(([k, v]) => (
            <div key={k} className="flex justify-between text-rose-700"><dt>{k}</dt><dd className="tabular-nums">−{formatINR(v)}</dd></div>
          ))}
        </dl>
      )}
      {card.difference_note && (
        <p className="mt-3 flex gap-1.5 rounded-lg bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
          <TriangleAlert className="h-3.5 w-3.5 shrink-0" />{card.difference_note}
        </p>
      )}
    </Shell>
  );
}

export function DiffTable({ rows }: { rows: Card[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-left text-[10px] uppercase text-slate-400">
          <tr><th className="py-1">Item</th><th className="text-right">Before</th><th className="text-right">After</th><th className="text-right">Difference</th></tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r) => {
            const strong = ["net", "gross"].includes(r.key);
            const d = Number(r.difference);
            return (
              <tr key={r.key} className={strong ? "font-semibold text-slate-900" : "text-slate-700"}>
                <td className="py-1.5">{r.label}</td>
                <td className="text-right tabular-nums">{money(r.before, r.unit)}</td>
                <td className="text-right tabular-nums">{money(r.after, r.unit)}</td>
                <td className={`text-right tabular-nums ${d > 0 ? "text-emerald-700" : d < 0 ? "text-rose-700" : "text-slate-400"}`}>{signed(r.difference, r.unit)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function CompareCard({ card }: { card: Card }) {
  return (
    <Shell icon={<GitCompareArrows className="h-4 w-4" />} title={`${card.employee_name} · ${card.first_label} vs ${card.second_label}`}
      subtitle={card.structure_only ? "Salary structure comparison" : "Recalculated with today's data and rules"}>
      <DiffTable rows={card.rows} />
      {card.causes?.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] font-semibold uppercase text-slate-500">Reasons found in the data</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-slate-700">{card.causes.map((c: Card, i: number) => <li key={i}>{c.text}</li>)}</ul>
        </div>
      )}
      {card.unexplained && <p className="mt-2 text-xs text-amber-700">No salary, attendance or rule change in the data explains this difference.</p>}
    </Shell>
  );
}

export function RulesCard({ card }: { card: Card }) {
  return (
    <Shell icon={<Scale className="h-4 w-4" />} title={`${card.topic} rules`} subtitle={`${card.month_label}${card.status ? ` · ${card.status}` : ""}`}>
      {card.versions && card.versions.map((v: Card) => (
        <dl key={v.version} className="mb-2 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
          <dt className="col-span-2 font-medium text-slate-800">{v.version} · {v.from} to {v.to}</dt>
          <dt className="text-slate-500">Employee</dt><dd>{v.employee_rate_percent}%</dd>
          <dt className="text-slate-500">Employer (incl. EPS {v.eps_rate_percent}%)</dt><dd>{v.employer_rate_percent}%</dd>
          <dt className="text-slate-500">PF wage ceiling</dt><dd>{formatINR(v.pf_wage_ceiling)}</dd>
        </dl>
      ))}
      {card.slabs && (
        <table className="w-full text-xs">
          <thead className="text-left text-[10px] uppercase text-slate-400"><tr><th className="py-1">Monthly wage</th><th>Gender</th><th className="text-right">PT</th></tr></thead>
          <tbody className="divide-y divide-slate-100">
            {card.slabs.map((s: Card, i: number) => (
              <tr key={i}><td className="py-1">{formatINR(s.above)} – {s.up_to === "no limit" ? "no limit" : formatINR(s.up_to)}</td><td>{s.gender}</td><td className="text-right">{formatINR(s.amount)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {card.topic === "ESIC" && <p className="text-xs">Employee {card.employee_rate_percent}% · Employer {card.employer_rate_percent}% · wages up to {formatINR(card.wage_threshold)}</p>}
      {card.topic === "TDS" && <p className="text-xs">{card.regime} regime · FY {card.financial_year} · standard deduction {formatINR(card.standard_deduction)}</p>}
      {card.applies_to && <p className="mt-2 text-[11px] text-slate-400">For {card.applies_to}</p>}
      {card.sources?.map((s: Card) => <p key={s.title} className="mt-1 text-[11px] text-slate-500">Source: {s.title} › {s.section.split(" › ").at(-1)} ({s.status})</p>)}
      <p className="mt-1 text-[11px] text-slate-400">{card.source}</p>
    </Shell>
  );
}

export function InfoCard({ card, onNavigate }: { card: Card; onNavigate?: (page: PageId) => void }) {
  return (
    <Shell icon={<Info className="h-4 w-4" />} title={card.title} subtitle={card.subtitle}>
      <dl className="divide-y divide-slate-100 text-xs">
        {card.fields.map((f: Card) => (
          <div key={f.label} className={`flex justify-between gap-3 py-1.5 ${f.strong ? "font-semibold" : ""}`}>
            <dt className="text-slate-600">{f.label}{f.hint && <span className="block text-[11px] font-normal text-slate-400">{f.hint}</span>}</dt>
            <dd className="text-right tabular-nums text-slate-900">{f.value}</dd>
          </div>
        ))}
      </dl>
      {card.notes?.length > 0 && <ul className="mt-2 space-y-0.5 text-[11px] text-slate-500">{card.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>}
      {card.link && onNavigate && (
        <button type="button" onClick={() => onNavigate(card.link.page)}
          className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800">
          {card.link.label} <ArrowRight className="h-3.5 w-3.5" />
        </button>
      )}
    </Shell>
  );
}

export function ActionPreviewCard({ card, token }: { card: Card; token: string }) {
  const [open, setOpen] = useState(false);
  const [applied, setApplied] = useState<string | null>(null);
  const proposal = card.proposal ?? null;
  return (
    <>
    <Shell icon={<ShieldCheck className="h-4 w-4" />} title={`Preview · ${card.employee_name}`} subtitle={`${card.title} · ${card.period_label}`}>
      <p className="mb-2 rounded-lg bg-indigo-50 px-2.5 py-1.5 text-xs text-indigo-800">Preview only — nothing has been changed.</p>
      <DiffTable rows={card.rows} />
      {card.overtime?.map((o: Card) => <p key={o.work_date} className="mt-2 text-xs text-slate-600">Overtime {o.work_date}: {o.formula} <span className="text-slate-400">({o.rate_source})</span></p>)}
      {card.assumptions?.length > 0 && (
        <ul className="mt-2 list-disc pl-4 text-[11px] text-slate-500">{card.assumptions.map((a: string) => <li key={a}>{a}</li>)}</ul>
      )}
      {card.warnings?.length > 0 && (
        <div className="mt-2 space-y-1 rounded-lg bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
          {card.warnings.map((w: string) => <p key={w} className="flex gap-1.5"><TriangleAlert className="h-3.5 w-3.5 shrink-0" />{w}</p>)}
        </div>
      )}
      <div className="mt-3 flex justify-end gap-2">
        {applied ? (
          <span className="rounded-lg bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700">
            {applied === "PENDING_APPROVAL" ? "Sent for approval" : applied === "APPLIED" ? "Applied" : "Not applied"}
          </span>
        ) : (
          <button type="button" onClick={() => setOpen(true)} disabled={!proposal}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">Review &amp; Apply</button>
        )}
      </div>
    </Shell>
    {open && proposal && (
      <ChangeDialog token={token} kind={kindOf(proposal.change)} employeeCode={proposal.employee_code}
        employeeName={card.employee_name} month={proposal.month} initial={proposal.change}
        source="chat" onClose={() => setOpen(false)} onApplied={(r) => setApplied(r.status)} />
    )}
    </>
  );
}

export function KnowledgeCard({ card }: { card: Card }) {
  return (
    <Shell icon={<BookOpen className="h-4 w-4" />} title="From the knowledge base" subtitle="Quoted from policy documents - no employee data">
      <ol className="space-y-3">
        {card.passages.map((p: Card, i: number) => (
          <li key={i} className="text-xs">
            <p className="font-semibold text-slate-800">[{i + 1}] {p.title} <span className="font-normal text-slate-400">› {p.section.split(" › ").slice(1).join(" › ")}</span></p>
            <p className="mt-0.5 text-slate-700">{p.snippet}</p>
            <p className="mt-0.5 text-[11px] text-slate-400">{p.status} · {p.jurisdiction}{p.effective_from ? ` · from ${p.effective_from}` : ""} · {p.source}</p>
          </li>
        ))}
      </ol>
    </Shell>
  );
}
