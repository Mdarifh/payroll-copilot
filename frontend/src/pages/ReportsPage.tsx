import { ChartColumn, Download, FileSpreadsheet, FileText, Info } from "lucide-react";
import { useEffect, useState } from "react";
import { useToast } from "../components/layout/Toast";
import { ApiError, directoryFilters, downloadReport, getReport, listReports, type ReportData } from "../lib/api";
import { formatINR } from "../lib/format";

// Display only: every number comes from the saved payslips; downloads are audited.

function lastMonth() {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function cell(kind: string, v: string | number | null | undefined) {
  if (v == null || v === "") return "—";
  return kind === "money" ? formatINR(String(v)) : String(v);
}

export function ReportsPage({ token, onExpired }: { token: string; onExpired: () => void }) {
  const toast = useToast();
  const [reports, setReports] = useState<{ key: string; title: string }[]>([]);
  const [departments, setDepartments] = useState<string[]>([]);
  const [key, setKey] = useState("payroll_register");
  const [month, setMonth] = useState(lastMonth);
  const [department, setDepartment] = useState("");
  const [data, setData] = useState<ReportData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    listReports(token).then(setReports).catch((e) => e instanceof ApiError && e.status === 401 && onExpired());
    directoryFilters(token).then((f) => setDepartments(f.departments)).catch(() => undefined);
  }, [token, onExpired]);

  useEffect(() => {
    if (!month) return;
    let live = true;
    setData(null);
    setError(null);
    getReport(token, key, month, department)
      .then((d) => { if (live) setData(d); })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onExpired();
        if (live) setError(e instanceof ApiError ? e.message : "Could not load the report");
      });
    return () => { live = false; };
  }, [token, key, month, department, onExpired]);

  async function download(format: "csv" | "xlsx" | "pdf") {
    setBusy(format);
    try {
      await downloadReport(token, key, month, department, format);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Download failed", "error");
    } finally {
      setBusy(null);
    }
  }

  const field = "rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs outline-none focus:border-indigo-500";
  const button = "inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50";
  return (
    <div className="grid gap-4 p-4 sm:p-6 lg:grid-cols-[220px_1fr]">
      <nav className="rounded-2xl border border-slate-200 bg-white p-2 shadow-sm lg:self-start">
        {reports.map((r) => (
          <button key={r.key} type="button" onClick={() => setKey(r.key)}
            className={`block w-full rounded-lg px-3 py-2 text-left text-sm ${key === r.key ? "bg-indigo-50 font-medium text-indigo-700" : "text-slate-700 hover:bg-slate-50"}`}>
            {r.title}
          </button>
        ))}
      </nav>

      <section className="min-w-0 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-end gap-2">
          <p className="mr-auto flex items-center gap-2 text-sm font-semibold text-slate-900">
            <ChartColumn className="h-4 w-4 text-slate-400" />{data?.title ?? reports.find((r) => r.key === key)?.title ?? "Report"}
          </p>
          <label className="text-[11px] text-slate-500">Month <input type="month" value={month} onChange={(e) => setMonth(e.target.value)} className={field} /></label>
          <select value={department} onChange={(e) => setDepartment(e.target.value)} aria-label="Department" className={field}>
            <option value="">All departments</option>
            {departments.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <button type="button" disabled={!data || !!busy} onClick={() => download("csv")} className={button}><Download className="h-3.5 w-3.5" />CSV</button>
          <button type="button" disabled={!data || !!busy} onClick={() => download("xlsx")} className={button}><FileSpreadsheet className="h-3.5 w-3.5" />Excel</button>
          <button type="button" disabled={!data || !!busy} onClick={() => download("pdf")} className={button}><FileText className="h-3.5 w-3.5" />PDF</button>
        </div>

        {error ? <p role="alert" className="mt-4 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>
          : !data ? <div className="mt-4 h-48 animate-pulse rounded-xl bg-slate-100" />
            : (
              <>
                {data.notes.map((n) => <p key={n} className="mt-3 flex gap-1.5 text-xs text-slate-500"><Info className="h-3.5 w-3.5 shrink-0" />{n}</p>)}
                {data.rows.length === 0 ? (
                  <p className="py-10 text-center text-sm text-slate-500">No data for {data.period}. Run payroll for this month first.</p>
                ) : (
                  <div className="mt-3 max-h-[65vh] overflow-auto rounded-xl border border-slate-200">
                    <table className="w-full text-xs">
                      <thead className="sticky top-0 bg-slate-50 text-left text-[11px] uppercase text-slate-500">
                        <tr>{data.columns.map((c) => <th key={c.key} className={`whitespace-nowrap px-3 py-2 ${c.kind === "text" ? "" : "text-right"}`}>{c.label}</th>)}</tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {data.rows.map((r, i) => (
                          <tr key={i} className="hover:bg-slate-50">
                            {data.columns.map((c) => <td key={c.key} className={`whitespace-nowrap px-3 py-1.5 ${c.kind === "text" ? "text-slate-700" : "text-right tabular-nums text-slate-800"}`}>{cell(c.kind, r[c.key])}</td>)}
                          </tr>
                        ))}
                      </tbody>
                      {Object.keys(data.totals).length > 0 && (
                        <tfoot className="sticky bottom-0 bg-slate-100 font-semibold text-slate-900">
                          <tr>{data.columns.map((c, i) => (
                            <td key={c.key} className={`whitespace-nowrap px-3 py-2 ${c.kind === "text" ? "" : "text-right tabular-nums"}`}>
                              {i === 0 ? `Total (${data.rows.length})` : c.key in data.totals ? cell(c.kind, data.totals[c.key]) : ""}
                            </td>
                          ))}</tr>
                        </tfoot>
                      )}
                    </table>
                  </div>
                )}
              </>
            )}
      </section>
    </div>
  );
}
