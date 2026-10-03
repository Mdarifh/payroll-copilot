import { LeaveRequestCard, TaxCompareCard } from "./LeaveTaxCards";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { MessageSquare, Plus, Trash2 } from "lucide-react";
import { ApiError, chatSession, chatSessions, forgetChat, sendChat, type ChatSessionSummary } from "../lib/api";
import type { BotMessage, ChatItem, Payslip, Session } from "../lib/types";
import { BotIcon, SendIcon } from "./icons";
import { BulkLopCard } from "./BulkLopCard";
import { ActionPreviewCard, CompareCard, ExplainCard, InfoCard, KnowledgeCard, RulesCard } from "./InsightCards";
import { PayslipCard } from "./PayslipCard";
import type { PageId } from "../lib/nav";
import { isStaffRole } from "../lib/permissions";

// "send" chips ask at once; "fill" chips put an example in the box to edit first
type Chip = { label: string; message: string; fill?: boolean };
const EMPLOYEE_CHIPS: Chip[] = [
  { label: "Payslip", message: "Show my latest payslip" },
  { label: "Leave balance", message: "What is my leave balance?" },
  { label: "Tax summary", message: "Show my tax summary for this financial year" },
  { label: "Explain my PF", message: "How is my PF calculated?" },
  { label: "Compare my pay", message: "Compare my pay for last month and the month before", fill: true },
];
const HR_CHIPS: Chip[] = [
  { label: "Explain PF", message: "Explain PF rules" },
  { label: "Explain ESIC", message: "Explain ESIC rules" },
  { label: "Explain PT", message: "Explain PT rules" },
  { label: "Explain TDS", message: "Explain TDS rules" },
  { label: "Check employee", message: "Show details of EMP-101", fill: true },
  { label: "Compare salary", message: "Compare EMP-101 August vs September", fill: true },
  { label: "Explain salary change", message: "Why did EMP-101's net pay change in September?", fill: true },
  { label: "Calculate overtime", message: "Add 4 hours overtime for EMP-101", fill: true },
  { label: "Find payroll issues", message: "Find payroll issues in September 2026" },
  { label: "Find negative arrears", message: "Find negative arrears in September 2026" },
  { label: "Run AI Payroll Audit", message: "Run AI payroll audit for September 2026" },
  { label: "Payroll readiness", message: "Run September 2026 payroll" },
];

let nextId = 0;
const newId = () => `m${++nextId}`;

function BotBubble({ message, token, onNavigate }: {
  message: BotMessage; token: string; onNavigate?: (page: PageId) => void;
}) {
  if (message.type === "text") {
    return (
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-800 shadow-sm">
        {(message as { text: string }).text}
      </div>
    );
  }
  if (message.type === "payslip_card") {
    const slip = message as { payslip: Payslip; download_url: string | null };
    return <PayslipCard payslip={slip.payslip} downloadUrl={slip.download_url} token={token} />;
  }
  const card = message as Record<string, unknown>;
  if (message.type === "explain_card") return <ExplainCard card={card} />;
  if (message.type === "compare_card") return <CompareCard card={card} />;
  if (message.type === "rules_card") return <RulesCard card={card} />;
  if (message.type === "knowledge_card") return <KnowledgeCard card={card} />;
  if (message.type === "info_card") return <InfoCard card={card} onNavigate={onNavigate} />;
  if (message.type === "action_preview") return <ActionPreviewCard card={card} token={token} />;
  if (message.type === "leave_request_card") return <LeaveRequestCard card={card} token={token} />;
  if (message.type === "tax_compare_card") return <TaxCompareCard card={card} token={token} />;
  if (message.type === "bulk_lop_card") {
    return <BulkLopCard batchId={String(message.batch_id)} token={token} />;
  }
  return ( // other card types are not supported yet
    <div className="rounded-xl border border-dashed border-slate-300 px-3 py-2 text-xs text-slate-500">
      This kind of card ({message.type}) is not supported yet.
    </div>
  );
}

export function ChatPage({ session, onExpired, ask, onNavigate }: {
  session: Session; onExpired: () => void; onNavigate?: (page: PageId) => void;
  /** a question sent from elsewhere (Overview, Ctrl+K); id changes for every new ask */
  ask?: { id: number; text: string };
}) {
  const { user, token } = session;
  const greeting: ChatItem = {
    id: "hello", from: "bot",
    message: { type: "text", text: `Hello ${user.full_name.split(" ")[0]}! Ask me about your payslip, leave or tax. Tap a button below or type your question.` },
  };
  const [items, setItems] = useState<ChatItem[]>([greeting]);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const refreshSessions = () => chatSessions(token).then(setSessions).catch(() => undefined);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per login
  useEffect(() => { void refreshSessions(); }, [token]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const chips = isStaffRole(user.role) ? [...EMPLOYEE_CHIPS, ...HR_CHIPS] : EMPLOYEE_CHIPS;

  useEffect(() => {
    // block body: newer browsers return a Promise from scrollIntoView, and an effect
    // may only return a clean-up function
    void bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, busy]);

  useEffect(() => {
    if (ask) void send(ask.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per ask id
  }, [ask?.id]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setInput("");
    setItems((prev) => [...prev, { id: newId(), from: "user", text: message }]);
    setBusy(true);
    const slowTimer = setTimeout(() => setSlow(true), 8000);
    try {
      const reply = await sendChat(token, message, conversationId);
      setConversationId(reply.conversation_id);
      void refreshSessions();
      setModel(reply.model);
      setItems((prev) => [...prev,
        ...reply.messages.map((m) => ({ id: newId(), from: "bot" as const, message: m }))]);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onExpired();
      const text = err instanceof ApiError ? err.message : "Something went wrong. Please try again.";
      setItems((prev) => [...prev, { id: newId(), from: "error", text }]);
    } finally {
      clearTimeout(slowTimer);
      setBusy(false);
      setSlow(false);
    }
  }

  function newChat() {  // the old chat stays in History
    setConversationId(null);
    setItems([greeting]);
    setShowHistory(false);
  }

  async function openChat(id: string) {
    try {
      const saved = await chatSession(token, id);
      const restored: ChatItem[] = [greeting];
      for (const it of saved.items) {
        if (it.from === "user") restored.push({ id: newId(), from: "user", text: it.text });
        else it.messages.forEach((m) => restored.push({ id: newId(), from: "bot", message: m }));
      }
      setItems(restored);
      setConversationId(id);
      setShowHistory(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onExpired();
    }
  }

  async function deleteChat(id: string) {
    await forgetChat(token, id).catch(() => undefined);
    if (id === conversationId) newChat();
    void refreshSessions();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(input);
  }

  const historyPanel = (
    <div className="flex min-h-0 flex-1 flex-col">
      <button type="button" onClick={newChat}
        className="m-3 inline-flex items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-700">
        <Plus className="h-3.5 w-3.5" />New chat
      </button>
      <p className="px-4 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">History</p>
      <ul className="flex-1 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 && <li className="px-2 py-3 text-xs text-slate-400">Your chats will appear here.</li>}
        {sessions.map((c) => (
          <li key={c.id} className={`group flex items-center rounded-lg ${c.id === conversationId ? "bg-indigo-50" : "hover:bg-slate-100"}`}>
            <button type="button" onClick={() => void openChat(c.id)} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left">
              <MessageSquare className="h-3.5 w-3.5 shrink-0 text-slate-400" />
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium text-slate-800">{c.title}</span>
                <span className="block text-[10px] text-slate-400">{c.updated_at ? new Date(c.updated_at).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : ""}</span>
              </span>
            </button>
            <button type="button" onClick={() => void deleteChat(c.id)} title="Delete chat" aria-label="Delete chat"
              className="mr-1 hidden rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600 group-hover:block"><Trash2 className="h-3.5 w-3.5" /></button>
          </li>
        ))}
      </ul>
    </div>
  );

  return (
    <div className="flex min-h-0 w-full flex-1">
    <aside className="hidden w-60 shrink-0 flex-col border-r border-slate-200 bg-white lg:flex">{historyPanel}</aside>
    {showHistory && (
      <div className="fixed inset-0 z-40 flex bg-slate-900/40 lg:hidden" onClick={() => setShowHistory(false)}>
        <aside className="flex w-72 flex-col bg-white" onClick={(e) => e.stopPropagation()}>{historyPanel}</aside>
      </div>
    )}
    <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col border-x border-slate-200 bg-slate-50">
      <header className="flex items-center justify-between gap-3 border-b border-slate-200 bg-white px-4 py-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-600 text-white">
            <BotIcon />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-slate-900">Payroll Assistant</h1>
            <p className="text-xs text-slate-500">Numbers come from the payroll engine</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => setShowHistory(true)}
            className="rounded-lg px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100 lg:hidden">
            History
          </button>
          <button type="button" onClick={newChat}
            className="rounded-lg px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100">
            New chat
          </button>
        </div>
      </header>

      <main className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
        {items.map((item) =>
          item.from === "user" ? (
            <div key={item.id} className="flex justify-end">
              <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-emerald-600 px-4 py-2.5 text-sm text-white">
                {item.text}
              </div>
            </div>
          ) : item.from === "error" ? (
            <div key={item.id} role="alert"
              className="max-w-[85%] rounded-2xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
              {item.text}
            </div>
          ) : (
            <div key={item.id} className="flex"><BotBubble message={item.message} token={token} onNavigate={onNavigate} /></div>
          ),
        )}
        {busy && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="flex gap-1">
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400 [animation-delay:150ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400 [animation-delay:300ms]" />
            </span>
            {slow && <span>Thinking… the first answer can take up to a minute.</span>}
          </div>
        )}
        <div ref={bottom} />
      </main>

      <footer className="border-t border-slate-200 bg-white px-4 pb-4 pt-3">
        <div className="-mx-1 mb-2.5 flex gap-2 overflow-x-auto px-1 pb-1">
          {chips.map((chip) => (
            <button key={chip.label} type="button" disabled={busy}
              onClick={() => (chip.fill ? setInput(chip.message) : void send(chip.message))}
              title={chip.fill ? "Puts an example in the box - edit it, then send" : chip.message}
              className="shrink-0 whitespace-nowrap rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-indigo-500 hover:text-indigo-700 disabled:opacity-50">
              {chip.label}
            </button>
          ))}
        </div>
        <form onSubmit={submit} className="flex gap-2">
          <input value={input} onChange={(e) => setInput(e.target.value)} maxLength={2000}
            placeholder="Type your question, e.g. Show my August payslip"
            className="flex-1 rounded-xl border border-slate-300 px-4 py-2.5 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100" />
          <button type="submit" disabled={busy || !input.trim()} aria-label="Send"
            className="flex items-center justify-center rounded-xl bg-emerald-600 px-4 text-white hover:bg-emerald-700 disabled:opacity-50">
            <SendIcon />
          </button>
        </form>
        {model && <p className="mt-2 text-center text-[11px] text-slate-400">
          {model === "intent-router" ? "Answered from payroll data by the intent router" : `Answers worded by ${model}`}
        </p>}
      </footer>
    </div>
    </div>
  );
}
