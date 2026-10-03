import { Sparkles } from "lucide-react";
import type { NavItem } from "../lib/nav";

export function ComingSoon({ item, onAsk }: { item: NavItem; onAsk: () => void }) {
  const Icon = item.icon;
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="max-w-md rounded-2xl border border-slate-200 bg-white p-8 text-center shadow-sm">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-600">
          <Icon className="h-6 w-6" />
        </span>
        <h2 className="mt-4 text-lg font-semibold text-slate-900">{item.label}</h2>
        <p className="mt-1.5 text-sm text-slate-500">{item.blurb}</p>
        <p className="mt-4 inline-block rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
          Arrives in Phase {item.phase}
        </p>
        <div className="mt-6">
          <button type="button" onClick={onAsk}
            className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700">
            <Sparkles className="h-4 w-4" /> Ask AI meanwhile
          </button>
        </div>
      </div>
    </div>
  );
}
