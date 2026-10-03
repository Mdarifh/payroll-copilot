import { useCallback, useEffect, useMemo, useState } from "react";
import { ChatPage } from "./components/ChatPage";
import { DirectoryPage } from "./components/DirectoryPage";
import { CommandPalette } from "./components/layout/CommandPalette";
import { Sidebar } from "./components/layout/Sidebar";
import { ToastProvider } from "./components/layout/Toast";
import { Topbar } from "./components/layout/Topbar";
import { LoginPage } from "./components/LoginPage";
import { listActions, loadSession, saveSession } from "./lib/api";
import { NAV, navFor, type PageId } from "./lib/nav";
import { PermsProvider, ROLE_PERMS, isStaffRole } from "./lib/permissions";
import type { Session } from "./lib/types";
import { useHashRoute } from "./lib/useHashRoute";
import { ComingSoon } from "./pages/ComingSoon";
import { ActionCenterPage } from "./pages/ActionCenterPage";
import { AiAuditPage } from "./pages/AiAuditPage";
import { AuditTimelinePage } from "./pages/AuditTimelinePage";
import { CompliancePage } from "./pages/CompliancePage";
import { KnowledgeBasePage } from "./pages/KnowledgeBasePage";
import { LeavePage } from "./pages/LeavePage";
import { OverviewPage } from "./pages/OverviewPage";
import { PayrollRunsPage } from "./pages/PayrollRunsPage";
import { ReportsPage } from "./pages/ReportsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SalaryStructuresPage } from "./pages/SalaryStructuresPage";

const PAGE_IDS = NAV.map((n) => n.id);

const PROMPTS: Record<"employee" | "manager" | "hr", string[]> = {
  employee: [
    "Show my latest payslip",
    "What is my leave balance?",
    "Apply casual leave tomorrow",
    "Which tax regime is better for me?",
  ],
  manager: [
    "Show my latest payslip",
    "What is my leave balance?",
    "Apply casual leave tomorrow",
    "Which tax regime is better for me?",
  ],
  hr: [
    "Rahul ka September PF kitna hai?",
    "Compare EMP-101 August vs September",
    "Run September 2026 payroll",
  ],
};

function Shell({ session, onLogout, onExpired }: {
  session: Session; onLogout: () => void; onExpired: () => void;
}) {
  const { user, token } = session;
  const items = useMemo(() => navFor(user.role), [user.role]);
  const [route, go] = useHashRoute<PageId>(PAGE_IDS, "overview");
  const page = items.some((i) => i.id === route) ? route : "overview";
  const [menuOpen, setMenuOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [ask, setAsk] = useState<{ id: number; text: string } | undefined>();
  const [overviewKey, setOverviewKey] = useState(0);
  const [pending, setPending] = useState(0);

  // pending approvals badge (HR): on load, every minute and after a decision
  const refreshPending = useCallback(() => {
    if (!isStaffRole(user.role)) return;
    listActions(token, "PENDING_APPROVAL", 1).then((d) => setPending(d.counts.PENDING_APPROVAL)).catch(() => undefined);
  }, [token, user.role]);
  useEffect(() => {
    refreshPending();
    const t = setInterval(refreshPending, 60_000);
    return () => clearInterval(t);
  }, [refreshPending]);

  const askAI = useCallback((text: string) => {
    setAsk((prev) => ({ id: (prev?.id ?? 0) + 1, text }));
    go("chat");
  }, [go]);

  const navigate = useCallback((id: PageId) => {
    if (id === "overview") setOverviewKey((k) => k + 1); // fresh numbers on every visit
    go(id);
  }, [go]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const current = items.find((i) => i.id === page)!;
  return (
    <div className="flex h-dvh bg-slate-50 text-slate-900">
      <Sidebar items={items} active={page} onNavigate={navigate} open={menuOpen} onClose={() => setMenuOpen(false)}
        badges={{ actions: pending }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar title={current.label} user={user} onMenu={() => setMenuOpen(true)}
          onSearch={() => setPaletteOpen(true)} onLogout={onLogout} />
        <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {page === "overview" && (
            <OverviewPage key={overviewKey} token={token} user={user} prompts={PROMPTS[isStaffRole(user.role) ? "hr" : user.role === "manager" ? "manager" : "employee"]}
              onAsk={askAI} onNavigate={navigate} onExpired={onExpired} />
          )}
          {/* chat and directory stay mounted so the conversation and selection survive navigation */}
          <div className={page === "chat" ? "flex min-h-0 flex-1" : "hidden"}>
            <ChatPage session={session} onExpired={onExpired} ask={ask} onNavigate={navigate} />
          </div>
          <div className={page === "employees" ? "flex min-h-0 flex-1" : "hidden"}>
            <DirectoryPage token={token} onExpired={onExpired} canEdit={ROLE_PERMS[user.role].includes("edit_attendance")} />
          </div>
          {page === "audit-timeline" && <AuditTimelinePage token={token} onExpired={onExpired} />}
          {page === "leave" && <LeavePage token={token} onExpired={onExpired} />}
          {page === "reports" && <ReportsPage token={token} onExpired={onExpired} />}
          {page === "settings" && <SettingsPage token={token} user={user} onExpired={onExpired} />}
          {page === "ai-audit" && <AiAuditPage token={token} onAsk={askAI} onNavigate={navigate} onExpired={onExpired} />}
          {page === "knowledge" && <KnowledgeBasePage token={token} user={user} onAsk={askAI} onExpired={onExpired} />}
          {page === "compliance" && <CompliancePage token={token} onAsk={askAI} onNavigate={navigate} onExpired={onExpired} />}
          {page === "actions" && <ActionCenterPage token={token} user={user} onExpired={onExpired} onChanged={refreshPending} />}
          {page === "payroll-runs" && <PayrollRunsPage token={token} onExpired={onExpired} />}
          {page === "structures" && <SalaryStructuresPage token={token} onExpired={onExpired} />}
          {current.phase && <ComingSoon item={current} onAsk={() => go("chat")} />}
        </main>
      </div>
      {paletteOpen && (
        <CommandPalette onClose={() => setPaletteOpen(false)} items={items} prompts={PROMPTS[isStaffRole(user.role) ? "hr" : user.role === "manager" ? "manager" : "employee"]}
          onNavigate={navigate} onAsk={askAI} />
      )}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession);
  const [notice, setNotice] = useState<string | undefined>();

  const update = useCallback((next: Session | null, message?: string) => {
    saveSession(next);
    setSession(next);
    setNotice(message);
  }, []);
  const expired = useCallback(
    () => update(null, "Your session expired. Please sign in again."), [update]);

  return (
    <ToastProvider>
      {session
        ? <PermsProvider role={session.user.role}><Shell key={session.user.employee_id} session={session} onLogout={() => update(null)} onExpired={expired} /></PermsProvider>
        : <LoginPage notice={notice} onLogin={(s) => update(s)} />}
    </ToastProvider>
  );
}
