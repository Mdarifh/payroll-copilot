import { CircleAlert, CircleCheck, Landmark, ScanSearch, ShieldCheck, Sparkles, TriangleAlert } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError, compliance, payrollMonths, type Compliance, type PayrollMonth, type RuleStatus } from "../lib/api";
import { formatINR } from "../lib/format";
import type { PageId } from "../lib/nav";

// Display only: rules, statuses and totals come from the backend configuration and payroll.

function Status({ s }: { s: string }) {
  const tone = s === "VERIFIED" || s === "PASS" ? "bg-emerald-50 text-emerald-700"
    : s === "MISSING" || s === "FAIL" ? "bg-rose-50 text-rose-700" : "bg-amber-50 text-amber-700";
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${tone}`}>{s}</span>;
}

function Card({ title, icon, action, children }: { title: string; icon: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-sm font-semibold text-slate-900">{icon}{title}</p>{action}
      </div>
      {children}
    </section>
  );
}

const ruleText = (r: RuleStatus | null | undefined) => (!r ? "—" : r.configured ? `${r.rule} · from ${r.effective_from}` : r.detail ?? "Not configured");

export function CompliancePage({ token, onAsk, onNavigate, onExpired }: {
  token: string; onAsk: (t: string) => void; onNavigate: (p: PageId) => void; onExpired: () => void;
}) {
  const [months, setMonths] = useState<PayrollMonth[]>([]);
  const [month, setMonth] = useState("");
  const [data, setData] = useState<Compliance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    payrollMonths(token).then((m) => { setMonths(m); setMonth((c) => c || m.find((x) => x.employees > 0)?.period || m[0]?.period || ""); })
      .catch((e) => (e instanceof ApiError && e.status === 401 ? onExpired() : setError("Could not load months")));
  }, [token, onExpired]);

  useEffect(() => {
    if (!month) return;
    setData(null);
    compliance(token, month).then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Could not load compliance"));
  }, [token, month]);

  const ask = (q: string) => <button type="button" onClick={() => onAsk(q)} className="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"><Sparkles className="h-3.5 w-3.5" />Ask rule</button>;

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">Statutory rules in force, their verification status, and this month's statutory totals.</p>
        <select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month" className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm">
          {months.map((m) => <option key={m.period} value={m.period}>{m.label}</option>)}
        </select>
      </div>

      {!data ? <div className="h-64 animate-pulse rounded-2xl bg-slate-200/60" /> : (
        <>
          {data.unverified.length > 0 && (
            <p className="flex gap-2 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
              <TriangleAlert className="h-4 w-4 shrink-0" />
              Rules marked UNVERIFIED must be checked against the official notification before live payroll: {data.unverified.join(", ")}.
            </p>
          )}
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Provident Fund" icon={<ShieldCheck className="h-4 w-4 text-slate-400" />} action={ask("Explain PF rules")}>
              {data.pf.error ? <p className="text-sm text-rose-700">{data.pf.error}</p> : (
                <table className="w-full text-xs">
                  <thead className="text-left text-[10px] uppercase text-slate-400"><tr><th className="py-1">Version</th><th>Period</th><th className="text-right">EE / ER</th><th className="text-right">Ceiling</th><th className="text-right">Status</th></tr></thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.pf.versions.map((v) => (
                      <tr key={v.version}><td className="py-1.5 font-medium">{v.version}</td><td>{v.from.slice(8)}–{v.to.slice(8)} {data.label}</td>
                        <td className="text-right">{v.employee_rate}% / {v.employer_rate}%</td><td className="text-right">{formatINR(v.pf_ceiling)}</td><td className="text-right"><Status s={v.status} /></td></tr>
                    ))}
                  </tbody>
                </table>
              )}
              {data.pf.split_month && <p className="mt-2 text-[11px] text-slate-500">The PF rule changes inside this month: PF is split by date.</p>}
            </Card>

            <Card title="ESIC and TDS" icon={<ShieldCheck className="h-4 w-4 text-slate-400" />} action={ask("Explain ESIC rules")}>
              <dl className="space-y-2 text-xs">
                <div className="flex items-center justify-between gap-2"><dt className="text-slate-600">ESIC · {data.esic.employees} covered employees</dt>
                  <dd className="flex items-center gap-2"><span className="text-slate-500">{ruleText(data.esic.rule)}</span>{data.esic.rule && <Status s={data.esic.rule.status} />}</dd></div>
                {data.tds.map((t) => (
                  <div key={t.regime} className="flex items-center justify-between gap-2"><dt className="text-slate-600">TDS {t.regime} regime · {t.employees} employees</dt>
                    <dd className="flex items-center gap-2"><span className="text-slate-500">{ruleText(t)}</span><Status s={t.status} /></dd></div>
                ))}
              </dl>
            </Card>

            <Card title="Professional Tax and LWF by state" icon={<Landmark className="h-4 w-4 text-slate-400" />} action={ask("Explain PT rules")}>
              <table className="w-full text-xs">
                <thead className="text-left text-[10px] uppercase text-slate-400"><tr><th className="py-1">State</th><th className="text-right">Employees</th><th>PT</th><th>LWF</th></tr></thead>
                <tbody className="divide-y divide-slate-100">
                  {data.states.map((s) => (
                    <tr key={s.state}><td className="py-1.5 font-medium">{s.state}</td><td className="text-right">{s.employees}</td>
                      <td className="pl-3">{s.pt.configured ? (s.pt.levied ? "Levied" : "Not levied") : "Missing"} <Status s={s.pt.status} /></td>
                      <td>{s.lwf.configured ? "Configured" : "Missing"} <Status s={s.lwf.status} /></td></tr>
                  ))}
                </tbody>
              </table>
            </Card>

            <Card title="EPF challan" icon={<Landmark className="h-4 w-4 text-slate-400" />}>
              {!data.challan ? <p className="text-xs text-slate-500">No payroll processed for {data.label} yet.</p> : (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  {data.challan.accounts.map((a) => (
                    <div key={a.account} className="col-span-2 flex justify-between"><dt className="text-slate-500">{a.account} · {a.name}</dt><dd className="tabular-nums">{formatINR(a.amount)}</dd></div>
                  ))}
                  <div className="col-span-2 flex justify-between border-t border-slate-100 pt-1 font-semibold"><dt>Total · {data.challan.contributing_members} members</dt><dd className="tabular-nums">{formatINR(data.challan.total)}</dd></div>
                </dl>
              )}
            </Card>
          </div>

          <Card title="Statutory health (latest AI audit)" icon={<ScanSearch className="h-4 w-4 text-slate-400" />}
            action={<button type="button" onClick={() => onNavigate("ai-audit")} className="text-xs font-medium text-indigo-600 hover:text-indigo-800">Open AI Audit</button>}>
            {!data.audit?.statutory ? <p className="text-xs text-slate-500">No audit yet for {data.label}. Run one from AI Audit.</p> : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {data.audit.statutory.map((c) => (
                  <div key={c.category} className="flex items-center gap-2 rounded-xl border border-slate-100 px-3 py-2">
                    {c.status === "PASS" ? <CircleCheck className="h-4 w-4 text-emerald-600" /> : c.status === "FAIL" ? <CircleAlert className="h-4 w-4 text-rose-600" /> : <TriangleAlert className="h-4 w-4 text-amber-500" />}
                    <div><p className="text-xs font-medium">{c.category}</p><p className="text-[11px] text-slate-500">{c.failed ? `${c.failed} failed` : c.warnings ? `${c.warnings} warning` : "OK"}</p></div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
