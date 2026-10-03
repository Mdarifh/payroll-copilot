import { LogOut, Menu, Search } from "lucide-react";
import { initials } from "../../lib/format";
import { ROLE_LABEL } from "../../lib/permissions";
import type { User } from "../../lib/types";

export function Topbar({ title, user, onMenu, onSearch, onLogout }: {
  title: string; user: User; onMenu: () => void; onSearch: () => void; onLogout: () => void;
}) {
  const mac = navigator.platform.toLowerCase().includes("mac");
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-4 sm:px-6">
      <button type="button" onClick={onMenu} aria-label="Open menu"
        className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100 lg:hidden"><Menu className="h-5 w-5" /></button>
      <h1 className="truncate text-base font-semibold text-slate-900">{title}</h1>

      <button type="button" onClick={onSearch}
        className="ml-auto flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-sm text-slate-500 hover:border-slate-300 sm:w-64">
        <Search className="h-4 w-4" />
        <span className="hidden flex-1 text-left sm:block">Search or ask AI…</span>
        <kbd className="hidden rounded border border-slate-200 bg-white px-1.5 text-[10px] font-medium text-slate-500 sm:block">
          {mac ? "⌘" : "Ctrl"} K
        </kbd>
      </button>

      <div className="flex items-center gap-2" title={user.email}>
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-800 text-xs font-medium text-white">
          {initials(user.full_name)}
        </span>
        <div className="hidden leading-tight md:block">
          <p className="max-w-[10rem] truncate text-xs font-medium text-slate-800">{user.full_name}</p>
          <p className="text-[11px] text-slate-500">{ROLE_LABEL[user.role]}</p>
        </div>
      </div>
      <button type="button" onClick={onLogout} aria-label="Logout" title="Logout"
        className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800"><LogOut className="h-4 w-4" /></button>
    </header>
  );
}
