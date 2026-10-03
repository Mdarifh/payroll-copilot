import { CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Kind = "success" | "error" | "info";
interface Toast { id: number; kind: Kind; text: string }

const ToastContext = createContext<(text: string, kind?: Kind) => void>(() => undefined);
export const useToast = () => useContext(ToastContext);

const STYLE: Record<Kind, { icon: typeof Info; tone: string }> = {
  success: { icon: CircleCheck, tone: "text-emerald-600" },
  error: { icon: TriangleAlert, tone: "text-rose-600" },
  info: { icon: Info, tone: "text-indigo-600" },
};

let nextId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = (id: number) => setToasts((t) => t.filter((x) => x.id !== id));
  const show = useCallback((text: string, kind: Kind = "info") => {
    const id = ++nextId;
    setToasts((t) => [...t.slice(-3), { id, kind, text }]);
    setTimeout(() => dismiss(id), 4500);
  }, []);

  return (
    <ToastContext.Provider value={show}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
        aria-live="polite">
        {toasts.map((t) => {
          const { icon: Icon, tone } = STYLE[t.kind];
          return (
            <div key={t.id} role="status"
              className="pointer-events-auto flex items-start gap-2.5 rounded-xl border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 shadow-lg">
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${tone}`} />
              <span className="flex-1">{t.text}</span>
              <button type="button" onClick={() => dismiss(t.id)} aria-label="Dismiss"
                className="text-slate-400 hover:text-slate-600"><X className="h-4 w-4" /></button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
