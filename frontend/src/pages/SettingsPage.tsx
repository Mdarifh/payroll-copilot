import { Building2, KeyRound, Search, ShieldCheck, Users } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/layout/Toast";
import { ApiError, approvalSetting, companySettings, listUsers, setApprovalSetting, updateUser, type UserRow } from "../lib/api";
import { useCan } from "../lib/permissions";
import type { User } from "../lib/types";

// Company settings (read-only for payroll staff) and users & roles (Admin).

function Card({ title, icon, children, action }: { title: string; icon: ReactNode; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-2"><p className="flex items-center gap-2 text-sm font-semibold text-slate-900">{icon}{title}</p>{action}</div>
      {children}
    </section>
  );
}

const PERM_LABEL: Record<string, string> = {
  view_payroll: "View payroll", edit_attendance: "LOP / attendance / overtime", edit_pay: "One-off pay & salary",
  run_payroll: "Run payroll", approve: "Approve", view_audit: "Audit timeline", admin: "Settings & users",
};

export function SettingsPage({ token, user, onExpired }: { token: string; user: User; onExpired: () => void }) {
  const can = useCan();
  const toast = useToast();
  const [company, setCompany] = useState<Awaited<ReturnType<typeof companySettings>> | null>(null);
  const [approval, setApproval] = useState<boolean | null>(null);
  const [data, setData] = useState<Awaited<ReturnType<typeof listUsers>> | null>(null);
  const [search, setSearch] = useState("");
  const [edit, setEdit] = useState<{ user: UserRow; role: string; login: boolean } | null>(null);
  const [toggling, setToggling] = useState(false);

  useEffect(() => {
    companySettings(token).then(setCompany).catch((e) => e instanceof ApiError && e.status === 401 && onExpired());
    approvalSetting(token).then((r) => setApproval(r.change_approval)).catch(() => undefined);
  }, [token, onExpired]);
  const loadUsers = useCallback(() => {
    if (can("admin")) listUsers(token, search).then(setData).catch(() => undefined);
  }, [token, search, can]);
  useEffect(() => { const t = setTimeout(loadUsers, 250); return () => clearTimeout(t); }, [loadUsers]);

  const kv = (o: Record<string, unknown>) => (
    <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
      {Object.entries(o).map(([k, v]) => (
        <div key={k} className="flex justify-between gap-3 border-b border-slate-50 py-1"><dt className="text-slate-500">{k.replace(/_/g, " ")}</dt>
          <dd className="text-right font-medium text-slate-800">{Array.isArray(v) ? v.join(", ") || "—" : String(v ?? "—")}</dd></div>
      ))}
    </dl>
  );

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <Card title="Approval of payroll changes" icon={<ShieldCheck className="h-4 w-4 text-slate-400" />}
        action={approval != null && can("admin") && (
          <button type="button" role="switch" aria-checked={approval} onClick={() => setToggling(true)}
            className={`relative h-6 w-11 rounded-full transition ${approval ? "bg-indigo-600" : "bg-slate-300"}`}>
            <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition ${approval ? "left-5" : "left-0.5"}`} />
          </button>
        )}>
        <p className="text-xs text-slate-600">{approval == null ? "…" : approval ? "ON — LOP, attendance, overtime, one-off pay and salary changes wait for a second user with the Approve permission." : "OFF — confirmed changes apply at once (still recorded in the Action Center and audit log)."}</p>
      </Card>

      {can("admin") && (
        <Card title="Users and roles" icon={<Users className="h-4 w-4 text-slate-400" />}
          action={<label className="flex items-center gap-1.5 rounded-lg border border-slate-300 px-2"><Search className="h-3.5 w-3.5 text-slate-400" />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search" maxLength={100} className="h-8 w-40 bg-transparent text-xs outline-none" /></label>}>
          {!data ? <div className="h-40 animate-pulse rounded-xl bg-slate-100" /> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead className="text-left text-[11px] uppercase text-slate-500"><tr><th className="py-2">User</th><th>Role</th><th>Can</th><th>Login</th><th /></tr></thead>
                <tbody className="divide-y divide-slate-100">
                  {data.users.map((u) => (
                    <tr key={u.employee_code}>
                      <td className="py-2"><span className="font-medium text-slate-900">{u.full_name}</span><span className="block text-xs text-slate-400">{u.employee_code} · {u.email}</span></td>
                      <td className="text-xs font-medium text-slate-700">{u.role_label}</td>
                      <td className="max-w-xs text-[11px] text-slate-500">{u.permissions.length ? u.permissions.map((p) => PERM_LABEL[p] ?? p).join(" · ") : "Self-service only"}</td>
                      <td>{u.login_enabled ? <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700">active</span> : <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500">disabled</span>}</td>
                      <td className="text-right">{u.employee_code !== user.employee_code && (
                        <button type="button" onClick={() => setEdit({ user: u, role: u.role, login: u.login_enabled })}
                          className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"><KeyRound className="h-3 w-3" />Change</button>
                      )}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {company && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title={`Company payroll settings · ${company.company_name}`} icon={<Building2 className="h-4 w-4 text-slate-400" />}>{kv(company.payroll)}</Card>
          <Card title="Overtime policy" icon={<Building2 className="h-4 w-4 text-slate-400" />}>
            {kv(company.overtime_policy)}
            <p className="mt-2 text-[11px] text-slate-400">Edit config/payroll/overtime.yaml to change it.</p>
          </Card>
          <Card title="Leave entitlements (per financial year)" icon={<Building2 className="h-4 w-4 text-slate-400" />}>{kv(company.leave_entitlements)}</Card>
          <Card title="AI" icon={<Building2 className="h-4 w-4 text-slate-400" />}>{kv(company.llm)}
            <p className="mt-2 text-[11px] text-slate-400">The AI words answers; every number comes from the payroll engine.</p></Card>
        </div>
      )}

      {toggling && approval != null && (
        <ConfirmDialog title={approval ? "Turn approval OFF" : "Turn approval ON"} confirmLabel={approval ? "Turn off" : "Turn on"}
          tone={approval ? "rose" : "indigo"} reasonLabel="Reason (required, audited)"
          facts={[{ label: "Applies to", value: "LOP, attendance, overtime, one-off pay, salary" }, { label: "From", value: "now" }]}
          onClose={() => setToggling(false)}
          onConfirm={async (reason) => { const r = await setApprovalSetting(token, !approval, reason); setApproval(r.change_approval); toast(`Approval is now ${r.change_approval ? "ON" : "OFF"}`, "success"); }} />
      )}
      {edit && data && (
        <ConfirmDialog title={`Change ${edit.user.full_name}`} confirmLabel="Save" reasonLabel="Reason (required, audited)"
          facts={[{ label: "Current role", value: edit.user.role_label }, { label: "Login", value: edit.user.login_enabled ? "active" : "disabled" }]}
          warning="A role change takes effect at the user's next action - they are asked to log in again."
          onClose={() => setEdit(null)}
          onConfirm={async (reason) => {
            await updateUser(token, edit.user.employee_code, {
              ...(edit.role !== edit.user.role ? { role: edit.role } : {}),
              ...(edit.login !== edit.user.login_enabled ? { login_enabled: edit.login } : {}), reason });
            toast(`${edit.user.full_name} updated`, "success");
            loadUsers();
          }}>
          <div className="grid grid-cols-2 gap-3">
            <label className="text-xs font-medium text-slate-600">New role
              <select value={edit.role} onChange={(e) => setEdit({ ...edit, role: e.target.value })} className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-2 text-sm">
                {data.roles.map((r) => <option key={r.role} value={r.role}>{r.label}</option>)}
              </select>
            </label>
            <label className="flex items-end gap-2 pb-2 text-xs text-slate-700"><input type="checkbox" checked={edit.login} onChange={(e) => setEdit({ ...edit, login: e.target.checked })} />Login enabled</label>
            <p className="col-span-2 text-[11px] text-slate-500">This role can: {data.roles.find((r) => r.role === edit.role)?.permissions.map((p) => PERM_LABEL[p] ?? p).join(" · ") || "self-service only"}</p>
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
