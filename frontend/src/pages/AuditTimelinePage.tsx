import { ChevronDown, ChevronUp, History, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, auditTimeline, type TimelineEvent } from "../lib/api";

// Every recorded change, newest first - straight from the audit log (nothing inferred).

const GROUP_TONE: Record<string, string> = {
  payroll: "bg-indigo-500", lop: "bg-rose-500", attendance: "bg-amber-500", overtime: "bg-sky-500",
  adjustment: "bg-violet-500", salary: "bg-emerald-500", action: "bg-fuchsia-500", batch: "bg-slate-500",
  user: "bg-red-600", settings: "bg-red-600", knowledge: "bg-teal-500", ai_audit: "bg-cyan-600", leave: "bg-lime-500", report: "bg-slate-400",
};

function Event({ e }: { e: TimelineEvent }) {
  const [open, setOpen] = useState(false);
  const when = e.at ? new Date(e.at) : null;
  return (
    <li className="relative pl-8">
      <span className={`absolute left-[9px] top-1.5 h-3 w-3 rounded-full ring-4 ring-white ${GROUP_TONE[e.group] ?? "bg-slate-400"}`} />
      <button type="button" onClick={() => setOpen(!open)} className="w-full text-left">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="text-sm font-semibold text-slate-900">{e.label}
            {e.employee && <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600">{e.employee}</span>}
          </p>
          <span className="text-[11px] text-slate-400">{when ? when.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}</span>
        </div>
        {e.summary && <p className="text-xs text-slate-700">{e.summary}</p>}
        <p className="text-[11px] text-slate-500">{e.actor}{e.actor_role ? ` · ${e.actor_role}` : ""}{e.reason ? ` · “${e.reason}”` : ""}
          <span className="ml-1 inline-block align-middle text-slate-400">{open ? <ChevronUp className="inline h-3 w-3" /> : <ChevronDown className="inline h-3 w-3" />}</span></p>
      </button>
      {open && (
        <div className="mt-2 grid gap-2 rounded-xl bg-slate-50 p-3 text-[11px] sm:grid-cols-2">
          <div><p className="font-semibold text-slate-600">Before</p><pre className="whitespace-pre-wrap break-all text-slate-600">{e.old ? JSON.stringify(e.old, null, 1) : "—"}</pre></div>
          <div><p className="font-semibold text-slate-600">After</p><pre className="whitespace-pre-wrap break-all text-slate-600">{e.new ? JSON.stringify(e.new, null, 1) : "—"}</pre></div>
          <p className="text-slate-400 sm:col-span-2">{e.action} · {e.entity ?? ""} {e.entity_id ?? ""}{e.request_id ? ` · request ${e.request_id}` : ""}</p>
        </div>
      )}
    </li>
  );
}

export function AuditTimelinePage({ token, onExpired }: { token: string; onExpired: () => void }) {
  const [employee, setEmployee] = useState("");
  const [action, setAction] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [system, setSystem] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Awaited<ReturnType<typeof auditTimeline>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setPage(1); }, [employee, action, dateFrom, dateTo, system]);
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      auditTimeline(token, { employee, action, dateFrom, dateTo, system, page })
        .then((d) => { if (live) setData(d); })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) return onExpired();
          if (live) setError(e instanceof ApiError ? e.message : "Could not load the timeline");
        });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [token, employee, action, dateFrom, dateTo, system, page, onExpired]);

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  const field = "rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs outline-none focus:border-indigo-500";
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <section className="flex flex-wrap items-end gap-2 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <label className="flex items-center gap-1.5 rounded-lg border border-slate-300 px-2 focus-within:border-indigo-500">
          <Search className="h-3.5 w-3.5 text-slate-400" />
          <input value={employee} onChange={(e) => setEmployee(e.target.value)} placeholder="Employee code, e.g. EMP-101" maxLength={32} className="h-8 w-44 bg-transparent text-xs outline-none" />
        </label>
        <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="Type" className={field}>
          <option value="">All changes</option>
          {(data?.groups ?? []).map((g) => <option key={g} value={`${g}.`}>{g.replace("_", " ")}</option>)}
        </select>
        <label className="text-[11px] text-slate-500">From <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className={field} /></label>
        <label className="text-[11px] text-slate-500">To <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className={field} /></label>
        <label className="flex items-center gap-1.5 text-xs text-slate-600"><input type="checkbox" checked={system} onChange={(e) => setSystem(e.target.checked)} />Include chat and logins</label>
        {data && <span className="ml-auto text-xs text-slate-500">{data.total} events</span>}
      </section>

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <p className="mb-4 flex items-center gap-2 text-sm font-semibold text-slate-900"><History className="h-4 w-4 text-slate-400" />Timeline</p>
        {!data ? <div className="h-48 animate-pulse rounded-xl bg-slate-100" /> : data.items.length === 0 ? (
          <p className="py-10 text-center text-sm text-slate-500">No events match.</p>
        ) : (
          <ol className="relative space-y-5 before:absolute before:bottom-1 before:left-[14px] before:top-1 before:w-px before:bg-slate-200">
            {data.items.map((e) => <Event key={e.id} e={e} />)}
          </ol>
        )}
        {data && data.pages > 1 && (
          <div className="mt-4 flex items-center justify-between text-xs text-slate-600">
            <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">‹ Newer</button>
            <span>Page {data.page} of {data.pages}</span>
            <button type="button" disabled={page >= data.pages} onClick={() => setPage(page + 1)} className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40">Older ›</button>
          </div>
        )}
      </section>
    </div>
  );
}
