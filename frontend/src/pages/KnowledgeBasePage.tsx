import { BookOpen, FileText, RefreshCw, Search, Sparkles, Trash2, Upload, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/layout/Toast";
import {
  ApiError, kbDocument, kbDocuments, kbReindex, kbRemove, kbSearch, kbUpload,
  type KbDocument, type KbPassage,
} from "../lib/api";
import { ROLE_PERMS } from "../lib/permissions";
import type { User } from "../lib/types";

// Policies and rules the AI answers from. Employee and payroll data are never stored here.

const TYPE_LABEL: Record<string, string> = { policy: "Policy", rule: "Statutory rule", sop: "SOP", faq: "FAQ" };

function StatusPill({ status }: { status: string }) {
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${status === "VERIFIED" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{status}</span>;
}

function Passage({ p, onOpen }: { p: KbPassage; onOpen: (id: number) => void }) {
  return (
    <li className="py-3">
      <button type="button" onClick={() => onOpen(p.document_id)} className="text-left text-sm font-semibold text-indigo-700 hover:underline">{p.title}</button>
      <span className="ml-2 text-xs text-slate-400">{p.section.split(" › ").slice(1).join(" › ")}</span>
      <p className="mt-1 text-sm text-slate-700">{p.snippet}</p>
      <p className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <StatusPill status={p.status} /> {p.jurisdiction} · {p.effective_from ? `from ${p.effective_from}` : ""}{p.effective_to ? ` to ${p.effective_to}` : ""} · {p.source}
      </p>
    </li>
  );
}

function UploadDialog({ token, onClose, onDone }: { token: string; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [file, setFile] = useState<{ name: string; content: string } | null>(null);
  const [title, setTitle] = useState("");
  const [policyType, setPolicyType] = useState("payroll");
  const [docType, setDocType] = useState("policy");
  const [source, setSource] = useState("");
  const [roles, setRoles] = useState<string[]>(["employee", "manager", "hr"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const field = "mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      await kbUpload(token, { filename: file.name, content: file.content, title, policy_type: policyType, document_type: docType, source, access_roles: roles });
      toast(`“${title}” added to the knowledge base`, "success");
      onDone();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed");
    } finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => !busy && onClose()}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} className="w-full max-w-lg space-y-3 rounded-2xl bg-white p-5 shadow-2xl">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-900">Add a policy document</h2>
          <button type="button" onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100"><X className="h-4 w-4" /></button>
        </div>
        <label className="block text-xs font-medium text-slate-600">File (.md or .txt, up to 200 KB)
          <input type="file" accept=".md,.txt,text/plain,text/markdown" className={field} onChange={async (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            setFile({ name: f.name, content: await f.text() });
            if (!title) setTitle(f.name.replace(/\.(md|txt)$/i, "").replace(/[-_]/g, " "));
          }} />
        </label>
        <label className="block text-xs font-medium text-slate-600">Title<input value={title} onChange={(e) => setTitle(e.target.value)} className={field} maxLength={200} /></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs font-medium text-slate-600">Topic
            <select value={policyType} onChange={(e) => setPolicyType(e.target.value)} className={field}>
              {["payroll", "leave", "overtime", "attendance", "pf", "esic", "pt", "tds", "fnf", "reimbursement", "other"].map((t) => <option key={t}>{t}</option>)}
            </select>
          </label>
          <label className="block text-xs font-medium text-slate-600">Type
            <select value={docType} onChange={(e) => setDocType(e.target.value)} className={field}>
              {Object.entries(TYPE_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
        </div>
        <label className="block text-xs font-medium text-slate-600">Source (where this policy comes from)<input value={source} onChange={(e) => setSource(e.target.value)} placeholder="e.g. HR handbook 2026, section 4" className={field} maxLength={500} /></label>
        <fieldset className="text-xs text-slate-600">
          <legend className="font-medium">Who may read it</legend>
          <div className="mt-1 flex gap-4">
            {["employee", "manager", "hr"].map((r) => (
              <label key={r} className="flex items-center gap-1.5 capitalize">
                <input type="checkbox" checked={roles.includes(r)} disabled={r === "hr"}
                  onChange={(e) => setRoles(e.target.checked ? [...roles, r] : roles.filter((x) => x !== r))} />{r === "hr" ? "HR" : r}
              </label>
            ))}
          </div>
        </fieldset>
        <p className="text-[11px] text-slate-400">Documents with personal data (PAN, Aadhaar, emails, phone numbers) are refused. New uploads are marked UNVERIFIED.</p>
        {error && <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-600 hover:bg-slate-100">Cancel</button>
          <button type="submit" disabled={busy || !file || title.trim().length < 3 || source.trim().length < 3}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">{busy ? "Adding…" : "Add document"}</button>
        </div>
      </form>
    </div>
  );
}

export function KnowledgeBasePage({ token, user, onAsk, onExpired }: {
  token: string; user: User; onAsk: (text: string) => void; onExpired: () => void;
}) {
  const toast = useToast();
  const hr = ROLE_PERMS[user.role].includes("admin");
  const [docs, setDocs] = useState<KbDocument[] | null>(null);
  const [stats, setStats] = useState<{ documents: number; chunks: number; unverified: number } | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KbPassage[] | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [topic, setTopic] = useState("");
  const [open, setOpen] = useState<KbDocument | null>(null);
  const [uploading, setUploading] = useState(false);
  const [removing, setRemoving] = useState<KbDocument | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => kbDocuments(token).then((d) => { setDocs(d.documents); setStats(d.stats); }).catch((e) => {
    if (e instanceof ApiError && e.status === 401) return onExpired();
    setError(e instanceof ApiError ? e.message : "Could not load the knowledge base");
  }), [token, onExpired]);
  useEffect(() => { void load(); }, [load]);

  async function search(e?: FormEvent) {
    e?.preventDefault();
    if (query.trim().length < 2) return;
    const r = await kbSearch(token, query.trim());
    setResults(r.results);
    setAnswer(r.answer.found ? r.answer.text : null);
  }
  const openDoc = (id: number) => kbDocument(token, id).then(setOpen).catch(() => undefined);

  if (error) return <p role="alert" className="m-6 rounded-xl bg-rose-50 p-4 text-sm text-rose-700">{error}</p>;
  const topics = [...new Set((docs ?? []).map((d) => d.policy_type))].sort();
  const shown = (docs ?? []).filter((d) => !topic || d.policy_type === topic);

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <form onSubmit={search} className="flex gap-2">
          <label className="flex flex-1 items-center gap-2 rounded-lg border border-slate-300 px-3 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-100">
            <Search className="h-4 w-4 text-slate-400" />
            <input value={query} onChange={(e) => setQuery(e.target.value)} maxLength={300} placeholder="Search policies, e.g. kitni chhutti milti hai, overtime rate, PF ceiling"
              className="h-10 flex-1 bg-transparent text-sm outline-none" />
          </label>
          <button type="submit" className="rounded-lg bg-indigo-600 px-4 text-sm font-medium text-white hover:bg-indigo-700">Search</button>
          <button type="button" onClick={() => query.trim() && onAsk(query.trim())} title="Ask in AI Chat"
            className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-3 text-sm text-slate-700 hover:bg-slate-50"><Sparkles className="h-4 w-4" />Ask AI</button>
        </form>
        {results && (
          <div className="mt-4">
            {answer && <p className="rounded-xl bg-indigo-50 px-4 py-3 text-sm text-indigo-900">{answer}</p>}
            {results.length === 0 ? <p className="py-6 text-center text-sm text-slate-500">Nothing found for this question.</p>
              : <ul className="mt-2 divide-y divide-slate-100">{results.map((p) => <Passage key={`${p.document_id}-${p.section}`} p={p} onOpen={openDoc} />)}</ul>}
          </div>
        )}
      </section>

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-semibold text-slate-900"><BookOpen className="h-4 w-4 text-slate-400" />Documents</p>
            {stats && <p className="text-xs text-slate-500">{stats.documents} documents · {stats.chunks} passages indexed · {stats.unverified} unverified</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select value={topic} onChange={(e) => setTopic(e.target.value)} aria-label="Topic" className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs">
              <option value="">All topics</option>{topics.map((t) => <option key={t}>{t}</option>)}
            </select>
            {hr && (
              <>
                <button type="button" onClick={async () => { const r = await kbReindex(token); toast(`Re-indexed: ${r.added} added, ${r.updated} updated, ${r.removed} removed`, "success"); void load(); }}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-50"><RefreshCw className="h-3.5 w-3.5" />Re-index</button>
                <button type="button" onClick={() => setUploading(true)}
                  className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-indigo-700"><Upload className="h-3.5 w-3.5" />Add document</button>
              </>
            )}
          </div>
        </div>
        {!docs ? <div className="mt-4 h-40 animate-pulse rounded-xl bg-slate-100" /> : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="text-left text-[11px] uppercase text-slate-500">
                <tr><th className="py-2">Document</th><th>Topic</th><th>Type</th><th>Effective</th><th>Status</th><th>Readers</th><th /></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {shown.map((d) => (
                  <tr key={d.id}>
                    <td className="py-2"><button type="button" onClick={() => openDoc(d.id)} className="inline-flex items-center gap-1.5 text-left font-medium text-slate-800 hover:text-indigo-700"><FileText className="h-3.5 w-3.5 text-slate-400" />{d.title}</button>
                      {d.generated && <span className="ml-2 text-[10px] text-slate-400">from rule config</span>}</td>
                    <td className="text-xs text-slate-600">{d.policy_type}</td>
                    <td className="text-xs text-slate-600">{TYPE_LABEL[d.document_type] ?? d.document_type}</td>
                    <td className="text-xs text-slate-500">{d.effective_from ?? "—"}{d.effective_to ? ` → ${d.effective_to}` : ""}</td>
                    <td><StatusPill status={d.status} /></td>
                    <td className="text-xs text-slate-500">{d.access_roles.join(", ")}</td>
                    <td className="text-right">{hr && d.uploaded && (
                      <button type="button" title="Remove" onClick={() => setRemoving(d)} className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"><Trash2 className="h-3.5 w-3.5" /></button>
                    )}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => setOpen(null)}>
          <div onClick={(e) => e.stopPropagation()} className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl bg-white shadow-2xl">
            <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
              <div><h2 className="text-base font-semibold text-slate-900">{open.title}</h2>
                <p className="text-xs text-slate-500">{open.source} · <StatusPill status={open.status} /></p></div>
              <button type="button" onClick={() => setOpen(null)} className="rounded p-1 text-slate-400 hover:bg-slate-100"><X className="h-4 w-4" /></button>
            </div>
            <pre className="flex-1 overflow-y-auto whitespace-pre-wrap px-5 py-4 font-sans text-sm text-slate-700">{open.content}</pre>
          </div>
        </div>
      )}
      {uploading && <UploadDialog token={token} onClose={() => setUploading(false)} onDone={() => void load()} />}
      {removing && (
        <ConfirmDialog title="Remove document" confirmLabel="Remove" tone="rose" reasonLabel="Reason (required)"
          facts={[{ label: "Document", value: removing.title }, { label: "File", value: removing.path }]}
          warning="The AI will no longer answer from this document."
          onClose={() => setRemoving(null)}
          onConfirm={async (reason) => { await kbRemove(token, removing.id, reason); toast("Document removed", "success"); void load(); }} />
      )}
    </div>
  );
}
