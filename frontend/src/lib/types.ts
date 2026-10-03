export type Role = "employee" | "manager" | "hr" | "admin" | "payroll_manager" | "payroll_executive" | "viewer";

export interface User {
  employee_id: number;
  employee_code: string;
  full_name: string;
  email: string;
  role: Role;
  department: string | null;
  designation: string | null;
}

export interface Session {
  token: string;
  expiresAt: string;
  user: User;
}

export interface PayLine {
  head_code: string;
  name: string;
  amount: string; // money is always a string from the API
}

export interface Payslip {
  employee_code: string;
  employee_name: string;
  designation: string | null;
  department: string | null;
  period: string;
  period_label: string;
  paid_days: string;
  basis_days: string;
  lop_days: string;
  earnings: PayLine[];
  deductions: PayLine[];
  employer_contributions: PayLine[];
  gross_earnings: string;
  total_deductions: string;
  net_pay: string;
  monthly_ctc: string;
  rule_versions: string[];
  warnings: string[];
}

export type BotMessage =
  | { type: "text"; text: string }
  | { type: "payslip_card"; payslip: Payslip; download_url: string }
  | { type: string; [key: string]: unknown }; // confirm_card / lop_card arrive in Phase 4

export interface ChatResponse {
  conversation_id: string;
  messages: BotMessage[];
  tools_used: string[];
  model: string | null;
  warnings: string[];
}

export type ChatItem =
  | { id: string; from: "user"; text: string }
  | { id: string; from: "bot"; message: BotMessage }
  | { id: string; from: "error"; text: string };
