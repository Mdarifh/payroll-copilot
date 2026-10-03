import { Calculator, Layers, Users } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import {
  ApiError, listStructures, structureDetail, structureEmployees, structurePreview,
  type StructureDetail, type StructurePreview, type StructureSummary,
} from "../lib/api";
import { formatINR } from "../lib/format";

// Display only: the breakup preview is computed by the backend payroll engine.

const TAX_STYLE: Record<string, string> = {
  Taxable: "bg-rose-50 text-rose-700 ring-rose-200",
  "Exempt with limit": "bg-amber-50 text-amber-700 ring-amber-200",
  "Exempt on bills": "bg-emerald-50 text-emerald-700 ring-emerald-200",
};

function Card({ title, icon: Icon, children, action }: {
  title: string; icon: typeof Layers; children: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Icon className="h-4 w-4 text-slate-400" />{title}</p>
        {action}
      </div>
      {children}
    </section>
  );
}

function Preview({ token, code }: { token: string; code: string }) {
  const [gross, setGross] = useState("50000");
  const [result, setResult] = useState<StructurePreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(event?: FormEvent) {
    event?.preventDefault();
    setError(null);
    try {
      setResult(await structurePreview(token, code, gross.replace(/[,\s₹]/g, "")));
    } catch (e) {
      setResult(null);
      setError(e instanceof ApiError ? e.message : "Could not calculate the breakup");
    }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- recalculate when the structure changes
  useEffect(() => { void run(); }, [code]);

  return (
    <Card title="Breakup preview" icon={Calculator}>
      <form onSubmit={run} className="flex gap-2">
        <label className="flex flex-1 items-center rounded-lg border border-slate-300 px-3 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-100">
          <span className="text-sm text-slate-400">₹</span>
          <input value={gross} onChange={(e) => setGross(e.target.value)} inputMode="decimal" maxLength={12}
            aria-label="Monthly gross" className="w-full bg-transparent px-2 py-2 text-sm outline-none" />
          <span className="whitespace-nowrap text-xs text-slate-400">/ month</span>
        </label>
        <button type="submit" className="rounded-lg bg-indigo-600 px-4 text-sm font-medium text-white hover:bg-indigo-700">Calculate</button>
      </form>
      {error && <p role="alert" className="mt-3 text-sm text-rose-600">{error}</p>}
      {result && (
        <table className="mt-3 w-full text-sm">
          <tbody className="divide-y divide-slate-100">
            {result.lines.map((l) => (
              <tr key={l.head_code}>
                <td className="py-2 text-slate-700">{l.name}<span className="block text-[11px] text-slate-400">{l.basis}</span></td>
                <td className="py-2 text-right tabular-nums">{formatINR(l.amount)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t-2 border-slate-200 font-semibold">
              <td className="py-2">Monthly gross</td><td className="py-2 text-right tabular-nums">{formatINR(result.total)}</td>
            </tr>
            <tr className="text-xs text-slate-500">
              <td className="pb-1">Annual gross</td><td className="pb-1 text-right tabular-nums">{formatINR(result.annual_gross)}</td>
            </tr>
          </tfoot>
        </table>
      )}
      <p className="mt-2 text-[11px] text-slate-400">Calculated by the payroll engine, not by AI.</p>
    </Card>
  );
}

function Members({ token, code }: { token: string; code: string }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Awaited<ReturnType<typeof structureEmployees>> | null>(null);
  useEffect(() => { setPage(1); }, [code]);
  useEffect(() => {
    let live = true;
    structureEmployees(token, code, page).then((d) => { if (live) setData(d); }).catch(() => undefined);
    return () => { live = false; };
  }, [token, code, page]);

  return (
    <Card title={`Employees on ${code}${data ? ` · ${data.total}` : ""}`} icon={Users}>
      {!data ? <div className="h-32 animate-pulse rounded-lg bg-slate-100" /> : data.items.length === 0 ? (
        <p className="py-6 text-center text-sm text-slate-400">No employee is on this structure.</p>
      ) : (
        <>
          <ul className="divide-y divide-slate-100">
            {data.items.map((e) => (
              <li key={e.employee_code} className="flex items-center justify-between py-2 text-sm">
                <span>
                  <span className="font-medium text-slate-800">{e.full_name}</span>
                  <span className="block text-xs text-slate-400">{e.employee_code} · {e.department ?? "—"}</span>
                </span>
                <span className="text-right tabular-nums text-slate-700">
                  {e.monthly_gross ? formatINR(e.monthly_gross) : "—"}
                  <span className="block text-[11px] text-slate-400">since {e.effective_from}</span>
                </span>
              </li>
            ))}
          </ul>
          {data.pages > 1 && (
            <div className="mt-2 flex items-center justify-between text-xs text-slate-600">
              <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Prev</button>
              <span>Page {data.page} of {data.pages}</span>
              <button type="button" disabled={page >= data.pages} onClick={() => setPage(page + 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next ›</button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

export function SalaryStructuresPage({ token, onExpired }: { token: string; onExpired: () => void }) {
  const [list, setList] = useState<StructureSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<StructureDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listStructures(token).then((d) => {
      setList(d.items);
      setSelected((cur) => cur ?? d.items[0]?.code ?? null);
    }).catch((e) => {
      if (e instanceof ApiError && e.status === 401) return onExpired();
      setError(e instanceof ApiError ? e.message : "Could not load salary structures");
    });
  }, [token, onExpired]);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    setDetail(null);
    structureDetail(token, selected).then((d) => { if (live) setDetail(d); })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : "Could not load the structure"); });
    return () => { live = false; };
  }, [token, selected]);

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {!list && Array.from({ length: 2 }, (_, i) => <div key={i} className="h-28 animate-pulse rounded-2xl bg-slate-200/70" />)}
        {list?.map((s) => (
          <button key={s.code} type="button" onClick={() => setSelected(s.code)}
            className={`rounded-2xl border bg-white p-4 text-left shadow-sm transition ${selected === s.code
              ? "border-indigo-400 ring-2 ring-indigo-100" : "border-slate-200 hover:border-slate-300"}`}>
            <div className="flex items-center justify-between">
              <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 text-[11px] font-semibold text-indigo-700 ring-1 ring-indigo-200">{s.code}</span>
              <span className="text-[11px] text-slate-400">v{s.version}</span>
            </div>
            <p className="mt-2 text-sm font-semibold text-slate-900">{s.name}</p>
            <p className="mt-1 text-xs text-slate-500">{s.components} components · {s.employees} employees · {s.departments} departments</p>
          </button>
        ))}
      </div>

      {selected && (
        <div className="grid gap-4 xl:grid-cols-5">
          <div className="space-y-4 xl:col-span-3">
            <Card title={detail ? `${detail.name} · components` : "Components"} icon={Layers}>
              {!detail ? <div className="h-40 animate-pulse rounded-lg bg-slate-100" /> : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-[11px] uppercase tracking-wide text-slate-500">
                      <tr><th className="py-2 font-medium">Component</th><th className="py-2 font-medium">Basis</th>
                        <th className="py-2 font-medium">Tax</th><th className="py-2 font-medium">Wage for</th></tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {detail.components.map((c) => (
                        <tr key={c.head_code}>
                          <td className="py-2.5 font-medium text-slate-800">{c.name}
                            <span className="block text-[11px] font-normal text-slate-400">{c.tab === "ALLOWANCES" ? "Allowances tab" : "Earnings tab"}</span></td>
                          <td className="py-2.5 text-xs text-slate-600">{c.basis}</td>
                          <td className="py-2.5">
                            <span title={c.tax_note ?? undefined}
                              className={`whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${TAX_STYLE[c.tax_treatment ?? ""] ?? TAX_STYLE.Taxable}`}>
                              {c.tax_treatment}
                            </span>
                          </td>
                          <td className="py-2.5 text-[11px] text-slate-500">
                            {[c.pf_wage && "PF", c.esic_wage && "ESIC"].filter(Boolean).join(" · ") || "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Members token={token} code={selected} />
          </div>
          <div className="xl:col-span-2"><Preview token={token} code={selected} /></div>
        </div>
      )}
    </div>
  );
}
