import {
  BookOpen, CalendarDays, ChartColumn, History, Inbox, LayoutDashboard, Layers, CirclePlay, ScanSearch,
  Settings, ShieldCheck, Sparkles, Users, type LucideIcon,
} from "lucide-react";
import { ROLE_PERMS, type Perm } from "./permissions";
import type { Role } from "./types";

export type PageId =
  | "overview" | "chat" | "leave" | "payroll-runs" | "employees" | "structures" | "compliance"
  | "ai-audit" | "actions" | "reports" | "knowledge" | "audit-timeline" | "settings";

export interface NavItem {
  id: PageId;
  label: string;
  icon: LucideIcon;
  group: "Workspace" | "Payroll" | "Governance";
  /** permission needed to see the page (none = everyone) */
  perm?: Perm;
  /** set while the screen is not built yet: the phase that delivers it */
  phase?: number;
  blurb?: string;
}


export const NAV: NavItem[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard, group: "Workspace" },
  { id: "chat", label: "AI Chat", icon: Sparkles, group: "Workspace" },
  { id: "employees", label: "Employees", icon: Users, group: "Workspace" },
  { id: "leave", label: "Leave", icon: CalendarDays, group: "Workspace" },
  { id: "payroll-runs", label: "Payroll Runs", icon: CirclePlay, group: "Payroll", perm: "view_payroll" },
  { id: "structures", label: "Salary Structures", icon: Layers, group: "Payroll", perm: "view_payroll" },
  { id: "compliance", label: "Compliance", icon: ShieldCheck, group: "Payroll", perm: "view_payroll" },
  { id: "ai-audit", label: "AI Audit", icon: ScanSearch, group: "Payroll", perm: "view_payroll" },
  { id: "actions", label: "Action Center", icon: Inbox, group: "Governance", perm: "view_payroll" },
  { id: "reports", label: "Reports", icon: ChartColumn, group: "Governance", perm: "view_payroll",
    blurb: "Payroll, PF, ESIC, PT, TDS, LOP and variance reports with CSV and Excel export." },
  { id: "knowledge", label: "Knowledge Base", icon: BookOpen, group: "Governance" },
  { id: "audit-timeline", label: "Audit Timeline", icon: History, group: "Governance", perm: "view_audit" },
  { id: "settings", label: "Settings", icon: Settings, group: "Governance", perm: "view_payroll" },
];

export const navFor = (role: Role) => NAV.filter((n) => !n.perm || (ROLE_PERMS[role] ?? []).includes(n.perm));
