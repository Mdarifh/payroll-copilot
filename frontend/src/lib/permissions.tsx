import { createContext, useContext, type ReactNode } from "react";
import type { Role } from "./types";

// Mirror of src/payroll_copilot/auth/permissions.py. The server enforces every rule;
// the UI uses this only to hide buttons the server would refuse.

export type Perm = "view_payroll" | "edit_attendance" | "edit_pay" | "run_payroll" | "approve" | "view_audit" | "admin";

const VIEWER: Perm[] = ["view_payroll"];
const EXECUTIVE: Perm[] = [...VIEWER, "edit_attendance", "view_audit"];
const MANAGER: Perm[] = [...EXECUTIVE, "edit_pay", "run_payroll", "approve"];
const ALL: Perm[] = [...MANAGER, "admin"];

export const ROLE_PERMS: Record<Role, Perm[]> = {
  employee: [], manager: [], viewer: VIEWER, payroll_executive: EXECUTIVE,
  payroll_manager: MANAGER, admin: ALL, hr: ALL,
};

export const ROLE_LABEL: Record<Role, string> = {
  employee: "Employee", manager: "Line manager", hr: "HR (admin)", admin: "Admin",
  payroll_manager: "Payroll Manager", payroll_executive: "Payroll Executive", viewer: "Viewer",
};

export const isStaffRole = (role: Role) => ROLE_PERMS[role].includes("view_payroll");

const PermsContext = createContext<(p: Perm) => boolean>(() => false);

export function PermsProvider({ role, children }: { role: Role; children: ReactNode }) {
  const perms = ROLE_PERMS[role] ?? [];
  return <PermsContext.Provider value={(p) => perms.includes(p)}>{children}</PermsContext.Provider>;
}

/** can("approve") - may the logged-in user do this? */
export const useCan = () => useContext(PermsContext);
