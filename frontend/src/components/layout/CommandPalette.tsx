import { ArrowRight, Search, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { NavItem, PageId } from "../../lib/nav";

interface Command { key: string; label: string; hint: string; icon: NavItem["icon"]; run: () => void }

export function CommandPalette({ onClose, items, prompts, onNavigate, onAsk }: {
  onClose: () => void; items: NavItem[]; prompts: string[];
  onNavigate: (id: PageId) => void; onAsk: (text: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { input.current?.focus(); }, []);

  const commands = useMemo<Command[]>(() => {
    const q = query.trim().toLowerCase();
    const pages = items.filter((i) => !q || i.label.toLowerCase().includes(q)).map((i) => ({
      key: `page:${i.id}`, label: i.label, hint: "Go to", icon: i.icon, run: () => onNavigate(i.id),
    }));
    const asks = prompts.filter((p) => !q || p.toLowerCase().includes(q)).map((p) => ({
      key: `ask:${p}`, label: p, hint: "Ask AI", icon: Sparkles, run: () => onAsk(p),
    }));
    const free = q && !asks.some((a) => a.label.toLowerCase() === q)
      ? [{ key: "ask:free", label: query.trim(), hint: "Ask AI", icon: Sparkles, run: () => onAsk(query.trim()) }]
      : [];
    return [...pages, ...free, ...asks];
  }, [query, items, prompts, onNavigate, onAsk]);

  const choose = (c?: Command) => { if (c) { c.run(); onClose(); } };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/40 px-4 pt-[12vh]" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Command palette" onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-center gap-2.5 border-b border-slate-200 px-4">
          <Search className="h-4 w-4 text-slate-400" />
          <input ref={input} value={query} placeholder="Go to a page or ask the AI…"
            onChange={(e) => { setQuery(e.target.value); setIndex(0); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
              else if (e.key === "ArrowDown") { e.preventDefault(); setIndex((i) => Math.min(i + 1, commands.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setIndex((i) => Math.max(i - 1, 0)); }
              else if (e.key === "Enter") choose(commands[index]);
            }}
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400" />
          <kbd className="rounded border border-slate-200 px-1.5 text-[10px] text-slate-500">Esc</kbd>
        </div>
        <ul className="max-h-80 overflow-y-auto p-2">
          {commands.length === 0 && <li className="px-3 py-6 text-center text-sm text-slate-500">Nothing found.</li>}
          {commands.map((c, i) => (
            <li key={c.key}>
              <button type="button" onMouseEnter={() => setIndex(i)} onClick={() => choose(c)}
                className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm ${i === index ? "bg-indigo-50 text-indigo-900" : "text-slate-700"}`}>
                <c.icon className={`h-4 w-4 ${c.hint === "Ask AI" ? "text-indigo-500" : "text-slate-400"}`} />
                <span className="flex-1 truncate">{c.label}</span>
                <span className="text-[11px] text-slate-400">{c.hint}</span>
                {i === index && <ArrowRight className="h-3.5 w-3.5 text-indigo-500" />}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
