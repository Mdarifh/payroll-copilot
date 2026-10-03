import { Sparkles, X } from "lucide-react";
import type { NavItem, PageId } from "../../lib/nav";

const GROUPS = ["Workspace", "Payroll", "Governance"] as const;

export function Sidebar({ items, active, onNavigate, open, onClose, badges = {} }: {
  items: NavItem[]; active: PageId; onNavigate: (id: PageId) => void;
  open: boolean; onClose: () => void; badges?: Partial<Record<PageId, number>>;
}) {
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-slate-900/40 lg:hidden" onClick={onClose} />}
      <aside className={`fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col bg-[#0b1530] text-slate-300 transition-transform lg:static lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center justify-between px-5 py-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-500 text-white shadow-lg shadow-indigo-500/30">
              <Sparkles className="h-5 w-5" />
            </span>
            <div>
              <p className="text-sm font-semibold text-white">PayrollAI Copilot</p>
              <p className="text-[11px] text-slate-400">Ask · Analyze · Act · Verify</p>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close menu"
            className="rounded-lg p-1 text-slate-400 hover:bg-white/10 lg:hidden"><X className="h-5 w-5" /></button>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-6">
          {GROUPS.map((group) => {
            const groupItems = items.filter((i) => i.group === group);
            if (!groupItems.length) return null;
            return (
              <div key={group}>
                <p className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500">{group}</p>
                <ul className="space-y-0.5">
                  {groupItems.map(({ id, label, icon: Icon, phase }) => (
                    <li key={id}>
                      <button type="button" onClick={() => { onNavigate(id); onClose(); }}
                        aria-current={active === id ? "page" : undefined}
                        className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${active === id
                          ? "bg-indigo-500/15 font-medium text-white ring-1 ring-inset ring-indigo-400/30"
                          : "hover:bg-white/5 hover:text-white"}`}>
                        <Icon className={`h-4 w-4 ${active === id ? "text-indigo-300" : "text-slate-400"}`} />
                        <span className="flex-1 text-left">{label}</span>
                        {phase && <span className="rounded bg-white/5 px-1.5 text-[10px] text-slate-500">soon</span>}
                        {!!badges[id] && <span className="rounded-full bg-amber-400 px-1.5 text-[10px] font-semibold text-slate-900">{badges[id]}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </nav>
        <p className="border-t border-white/5 px-5 py-3 text-[11px] text-slate-500">
          Numbers come from the payroll engine, never from AI.
        </p>
      </aside>
    </>
  );
}
