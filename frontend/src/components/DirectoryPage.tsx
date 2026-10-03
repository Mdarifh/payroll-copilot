import { useCan } from "../lib/permissions";
import { useEffect, useState } from "react";
import {
  ApiError, directoryFilters, listEmployees, salaryDetails, structureDetail,
  type DirectoryFilters, type DirectoryPage as Page, type SalaryDetails, type SalaryRow,
  type StructureDetail,
} from "../lib/api";
import { formatINR, initials } from "../lib/format";
import { AttendanceTab } from "./AttendanceTab";
import { ChangeDialog } from "./ChangeDialog";

// Display only: every amount, total and basis comes from the backend.

const TAX_STYLE: Record<string, string> = {
  Taxable: "bg-rose-50 text-rose-700 ring-rose-200",
  "Exempt with limit": "bg-amber-50 text-amber-700 ring-amber-200",
  "Exempt on bills": "bg-emerald-50 text-emerald-700 ring-emerald-200",
};

function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100">
      <option value="">{label}: All</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

function StructurePill({ code, name, onOpen }: { code: string | null; name: string; onOpen?: () => void }) {
  const style = "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold ring-1";
  if (!code || !onOpen) {
    return <span className={`${style} bg-slate-100 text-slate-600 ring-slate-200`}>{code ?? name}</span>;
  }
  return (
    <button type="button" onClick={onOpen} title={`Open ${name}`}
      className={`${style} bg-indigo-50 text-indigo-700 ring-indigo-200 hover:bg-indigo-100`}>
      {code} <span aria-hidden>↗</span>
    </button>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-slate-400">{hint}</p>}
    </div>
  );
}

function SalaryTable({ rows, total, withTax }: { rows: SalaryRow[]; total: string; withTax?: boolean }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-2 font-medium">Head</th>
            <th className="px-4 py-2 font-medium">Basis</th>
            {withTax && <th className="px-4 py-2 font-medium">Tax treatment</th>}
            <th className="px-4 py-2 text-right font-medium">Monthly amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r) => (
            <tr key={r.head_code} className={r.in_structure ? "" : "text-slate-400"}>
              <td className="px-4 py-2.5 font-medium">{r.head}</td>
              <td className="px-4 py-2.5 text-xs">{r.basis}</td>
              {withTax && (
                <td className="px-4 py-2.5">
                  <span title={r.tax_note ?? undefined}
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${TAX_STYLE[r.tax_treatment ?? ""] ?? TAX_STYLE.Taxable}`}>
                    {r.tax_treatment}
                  </span>
                  {r.tax_note && <p className="mt-1 text-[11px] text-slate-400">{r.tax_note}</p>}
                </td>
              )}
              <td className="px-4 py-2.5 text-right tabular-nums">{formatINR(r.monthly_amount)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t-2 border-slate-200 bg-slate-50 font-semibold text-slate-900">
            <td className="px-4 py-2.5" colSpan={withTax ? 3 : 2}>Total</td>
            <td className="px-4 py-2.5 text-right tabular-nums">{formatINR(total)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function StructureModal({ token, code, onClose }: { token: string; code: string; onClose: () => void }) {
  const [data, setData] = useState<StructureDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    structureDetail(token, code).then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the structure"));
  }, [token, code]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Salary structure"
        className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
          <div>
            <p className="text-[11px] font-medium uppercase tracking-wide text-indigo-600">Salary structure</p>
            <h2 className="text-base font-semibold text-slate-900">{data?.name ?? code}</h2>
            {data && (
              <p className="text-xs text-slate-500">
                {data.code} · version {data.version} · used by {data.employees} employee
                {data.employees === 1 ? "" : "s"} in {data.departments} department{data.departments === 1 ? "" : "s"}
              </p>
            )}
          </div>
          <button type="button" onClick={onClose} aria-label="Close"
            className="rounded-lg px-2 py-1 text-slate-500 hover:bg-slate-100">✕</button>
        </div>
        <div className="px-5 py-4">
          {error && <p className="text-sm text-red-600">{error}</p>}
          {!data && !error && <p className="text-sm text-slate-500">Loading…</p>}
          {data && (
            <table className="w-full text-sm">
              <thead className="text-left text-[11px] uppercase tracking-wide text-slate-500">
                <tr><th className="py-1.5">Component</th><th className="py-1.5">Basis</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.components.map((c) => (
                  <tr key={c.head_code}>
                    <td className="py-2 font-medium text-slate-800">{c.name}</td>
                    <td className="py-2 text-slate-600">{c.basis}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function Details({ token, code, onOpenStructure, onExpired, canEdit }: {
  token: string; code: string; onOpenStructure: (code: string) => void; onExpired: () => void;
  canEdit: boolean;
}) {
  const [month, setMonth] = useState<string | undefined>();
  const [data, setData] = useState<SalaryDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"earnings" | "allowances" | "attendance">("earnings");
  const [revising, setRevising] = useState(false);
  const can = useCan();
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    salaryDetails(token, code, month)
      .then((d) => { if (live) { setData(d); setMonth(d.month); } })
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 401) return onExpired();
        setError(e instanceof ApiError ? e.message : "Could not load salary details");
      })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [token, code, month, onExpired, reload]);

  if (!data) {
    return <div className="p-8 text-sm text-slate-500">{error ?? "Loading…"}</div>;
  }
  const p = data.profile;
  return (
    <div className="space-y-5 p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-600 text-base font-semibold text-white">
            {initials(p.full_name)}
          </span>
          <div>
            <h2 className="text-lg font-semibold text-slate-900">{p.full_name}</h2>
            <p className="text-xs text-slate-500">
              {p.employee_code} · {p.designation ?? "—"}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-600">
              <span className="rounded-md bg-slate-100 px-2 py-0.5">{p.department ?? "No department"}</span>
              <span className="rounded-md bg-slate-100 px-2 py-0.5">{p.location ?? "No location"}</span>
              <StructurePill code={data.structure.code} name={data.structure.name}
                onOpen={data.structure.code ? () => onOpenStructure(data.structure.code!) : undefined} />
            </div>
          </div>
        </div>
        <label className="flex items-center gap-2 text-xs font-medium text-slate-600">
          Month
          <input type="month" value={month ?? ""} min={data.months.min} max={data.months.max}
            onChange={(e) => e.target.value && setMonth(e.target.value)}
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
        </label>
      </div>

      {loading && <p className="text-xs text-slate-400">Loading {month}…</p>}
      {error && <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      {!data.restricted && !error && (
        <>
          {canEdit && can("edit_pay") && (
            <div className="-mb-2 flex justify-end">
              <button type="button" onClick={() => setRevising(true)}
                className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50">Revise salary</button>
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Stat label="Annual gross" value={formatINR(data.annual_gross!)} />
            <Stat label="Monthly gross" value={formatINR(data.monthly_gross!)}
              hint={`Effective from ${data.effective_from}`} />
            <Stat label={`Net pay · ${data.month_label}`}
              value={data.net_pay!.amount ? formatINR(data.net_pay!.amount) : "—"} hint={data.net_pay!.source} />
          </div>
          {data.revised_in_month && (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Salary was revised during {data.month_label}:{" "}
              {data.revisions!.map((r) => `${formatINR(r.monthly_gross)} from ${r.effective_from}`).join(", ")}.
              The latest structure is shown.
            </p>
          )}
        </>
      )}

      <div>
        <div role="tablist" className="mb-3 flex gap-1 border-b border-slate-200">
          {(["earnings", "allowances", "attendance"] as const).map((t) => (
            <button key={t} role="tab" type="button" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium capitalize ${tab === t
                ? "border-indigo-600 text-indigo-700" : "border-transparent text-slate-500 hover:text-slate-800"}`}>
              {t}
            </button>
          ))}
        </div>
        {tab === "attendance" ? (
          <AttendanceTab token={token} code={code} month={month ?? data.month} onExpired={onExpired}
            canEdit={canEdit} employeeName={p.full_name} />
        ) : data.restricted ? (
          <p className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
            {data.message}
          </p>
        ) : error ? null : tab === "earnings"
          ? <SalaryTable rows={data.earnings!.rows} total={data.earnings!.total} />
          : <SalaryTable rows={data.allowances!.rows} total={data.allowances!.total} withTax />}
      </div>
      {revising && (
        <ChangeDialog token={token} kind="salary" employeeCode={code} employeeName={p.full_name}
          month={data.month} initial={{ salary: { monthly_gross: data.monthly_gross ?? "", effective_from: `${data.month}-01` }, reason: "" }}
          onClose={() => setRevising(false)} onApplied={() => setReload((k) => k + 1)} />
      )}
    </div>
  );
}

export function DirectoryPage({ token, onExpired, canEdit = false }: {
  token: string; onExpired: () => void; canEdit?: boolean;
}) {
  const [search, setSearch] = useState("");
  const [department, setDepartment] = useState("");
  const [location, setLocation] = useState("");
  const [structure, setStructure] = useState("");
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<DirectoryFilters | null>(null);
  const [list, setList] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [modal, setModal] = useState<string | null>(null);
  const query = useDebounced(search);

  useEffect(() => {
    directoryFilters(token).then(setFilters).catch(() => undefined);
  }, [token]);

  useEffect(() => setPage(1), [query, department, location, structure]);

  useEffect(() => {
    let live = true;
    listEmployees(token, { search: query, department, location, structure, page })
      .then((d) => {
        if (!live) return;
        setList(d);
        setError(null);
        setSelected((cur) => cur ?? d.items[0]?.employee_code ?? null);
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 401) return onExpired();
        setError(e instanceof ApiError ? e.message : "Could not load employees");
      });
    return () => { live = false; };
  }, [token, query, department, location, structure, page, onExpired]);

  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row">
      <aside className="flex max-h-[45vh] flex-col border-b border-slate-200 bg-white md:max-h-none md:w-80 md:border-b-0 md:border-r lg:w-96">
        <div className="space-y-2 border-b border-slate-200 p-3">
          <input value={search} onChange={(e) => setSearch(e.target.value)} maxLength={100}
            placeholder="Search name or employee ID"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
          <div className="flex gap-2">
            <Select label="Department" value={department} onChange={setDepartment}
              options={(filters?.departments ?? []).map((d) => ({ value: d, label: d }))} />
            <Select label="Location" value={location} onChange={setLocation}
              options={(filters?.locations ?? []).map((l) => ({ value: l, label: l }))} />
          </div>
          <Select label="Salary structure" value={structure} onChange={setStructure}
            options={(filters?.structures ?? []).map((s) => ({ value: s.code, label: `${s.code} · ${s.name}` }))} />
          <p className="text-xs text-slate-500">
            {list ? `${list.total} employee${list.total === 1 ? "" : "s"}` : "Loading…"}
          </p>
        </div>

        <ul className="flex-1 divide-y divide-slate-100 overflow-y-auto">
          {error && <li className="p-4 text-sm text-red-600">{error}</li>}
          {list?.items.length === 0 && <li className="p-4 text-sm text-slate-500">No employees match.</li>}
          {list?.items.map((e) => (
            <li key={e.employee_code}>
              <button type="button" onClick={() => setSelected(e.employee_code)}
                className={`flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-slate-50 ${selected === e.employee_code
                  ? "bg-indigo-50/70 ring-1 ring-inset ring-indigo-200" : ""}`}>
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-800 text-[11px] font-medium text-white">
                  {initials(e.full_name)}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-slate-900">{e.full_name}</span>
                  <span className="block truncate text-xs text-slate-500">
                    {e.employee_code} · {e.department ?? "—"}
                  </span>
                </span>
                <StructurePill code={e.structure_code} name={e.structure_name} />
              </button>
            </li>
          ))}
        </ul>

        {list && list.pages > 1 && (
          <div className="flex items-center justify-between border-t border-slate-200 px-3 py-2 text-xs text-slate-600">
            <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}
              className="rounded-md px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Prev</button>
            <span>Page {list.page} of {list.pages}</span>
            <button type="button" disabled={page >= list.pages} onClick={() => setPage(page + 1)}
              className="rounded-md px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Next ›</button>
          </div>
        )}
      </aside>

      <section className="min-h-0 flex-1 overflow-y-auto bg-slate-50">
        {selected
          ? <Details key={selected} token={token} code={selected} onOpenStructure={setModal} onExpired={onExpired} canEdit={canEdit} />
          : <p className="p-8 text-sm text-slate-500">Select an employee to see their salary structure.</p>}
      </section>

      {modal && <StructureModal token={token} code={modal} onClose={() => setModal(null)} />}
    </div>
  );
}
