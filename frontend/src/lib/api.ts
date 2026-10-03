import type { BotMessage, ChatResponse, Session, User } from "./types";

const BASE = "/api"; // proxied to the FastAPI backend by Vite
const STORAGE_KEY = "payroll-chatbot-session";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Session lives in sessionStorage: it ends when the browser tab is closed. */
export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as Session;
    return new Date(session.expiresAt) > new Date() ? session : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage blocked (private mode): the session just isn't remembered
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(BASE + path, { ...init, headers });
  } catch {
    throw new ApiError(0, "Cannot reach the payroll server. Is the API window still open?");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // HTML error page from the proxy when the API is down
      if (response.status >= 500) detail = "The payroll server is not running.";
    }
    throw new ApiError(response.status, typeof detail === "string" ? detail : "Request failed");
  }
  return (await response.json()) as T;
}

export async function login(email: string, password: string): Promise<Session> {
  const body = await request<{ access_token: string; expires_at: string; user: User }>(
    "/auth/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
  );
  return { token: body.access_token, expiresAt: body.expires_at, user: body.user };
}

export function sendChat(
  token: string,
  message: string,
  conversationId: string | null,
): Promise<ChatResponse> {
  return request<ChatResponse>(
    "/chat",
    {
      method: "POST",
      body: JSON.stringify(conversationId ? { message, conversation_id: conversationId } : { message }),
    },
    token,
  );
}

export interface ChatSessionSummary { id: string; title: string; updated_at: string | null; messages: number }

export const chatSessions = (token: string) => request<ChatSessionSummary[]>("/chat/sessions", {}, token);
export const chatSession = (token: string, id: string) =>
  request<{ id: string; title: string; items: ({ from: "user"; text: string } | { from: "bot"; messages: BotMessage[] })[] }>(
    `/chat/sessions/${encodeURIComponent(id)}`, {}, token);

export async function forgetChat(token: string, conversationId: string): Promise<void> {
  await request(`/chat/${encodeURIComponent(conversationId)}`, { method: "DELETE" }, token);
}

/** Download a file that needs the Authorization header (a plain link can't send it). */
export async function downloadFile(token: string, path: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(BASE + path, { headers: { Authorization: `Bearer ${token}` } });
  } catch {
    throw new ApiError(0, "Cannot reach the payroll server.");
  }
  if (!response.ok) throw new ApiError(response.status, "Download failed");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---------------------------------------------------------------- bulk payroll (HR)

export interface BatchStatus {
  batch_id: string;
  period: string;
  filters: Record<string, string>;
  status: "REVIEW" | "QUEUED" | "RUNNING" | "COMPLETED" | "COMPLETED_WITH_ERRORS" | "CANCELLED";
  total: number;
  ok_count: number;
  review_count: number;
  already_processed: number;
  processed: number;
  pending: number;
  done: number;
  skipped: number;
  failed: number;
  lop_applied: number;
  percent: number;
  finished: boolean;
  can_retry_failed: boolean;
  stage?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface LopItem {
  emp_id: string;
  name: string;
  department: string | null;
  work_state: string;
  absent_dates: string[];
  absent_days: string;
  suggested_days: string;
  lop_days: string;
  apply: boolean;
  per_day: string;
  divisor: string;
  deduction: string;
}

export interface LopTotals {
  selected: number;
  total_lop_days: string;
  total_deduction: string;
}

export interface LopPage {
  items: LopItem[];
  page: number;
  page_size: number;
  total_items: number;
  pages: number;
  totals: LopTotals;
  editable: boolean;
}

export const batchStatus = (token: string, id: string) =>
  request<BatchStatus>(`/batches/${encodeURIComponent(id)}/status`, {}, token);

export function lopItems(token: string, id: string, page: number, search: string, pageSize = 10) {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (search.trim()) query.set("search", search.trim());
  return request<LopPage>(`/batches/${encodeURIComponent(id)}/lop-items?${query}`, {}, token);
}

export const patchLopItem = (
  token: string,
  id: string,
  empId: string,
  change: { apply?: boolean; lop_days?: string },
) =>
  request<{ item: LopItem; totals: LopTotals }>(
    `/batches/${encodeURIComponent(id)}/lop-items/${encodeURIComponent(empId)}`,
    { method: "PATCH", body: JSON.stringify(change) },
    token,
  );

export const runBatch = (token: string, id: string, mode: "APPLY" | "SKIP_ALL", reason: string) =>
  request<BatchStatus>(
    `/batches/${encodeURIComponent(id)}/run`,
    { method: "POST", body: JSON.stringify({ mode, reason: reason || null }) },
    token,
  );

export const retryFailed = (token: string, id: string) =>
  request<BatchStatus>(`/batches/${encodeURIComponent(id)}/retry-failed`, { method: "POST" }, token);

// ---------------------------------------------------------------- employee directory

export interface DirectoryItem {
  employee_code: string;
  full_name: string;
  department: string | null;
  location: string | null;
  structure_code: string | null;
  structure_name: string;
}

export interface DirectoryPage {
  total: number;
  page: number;
  page_size: number;
  pages: number;
  items: DirectoryItem[];
}

export interface DirectoryFilters {
  departments: string[];
  locations: string[];
  structures: { code: string; name: string }[];
}

export interface SalaryRow {
  head_code: string;
  head: string;
  basis: string;
  monthly_amount: string;
  in_structure: boolean;
  tax_treatment?: string;
  tax_note?: string | null;
}

export interface SalaryDetails {
  profile: {
    employee_code: string; full_name: string; designation: string | null;
    department: string | null; location: string | null; doj: string;
  };
  month: string;
  month_label: string;
  months: { min: string; max: string };
  structure: { code: string | null; name: string };
  restricted: boolean;
  message?: string;
  effective_from?: string;
  revised_in_month?: boolean;
  revisions?: { effective_from: string; monthly_gross: string }[];
  monthly_gross?: string;
  annual_gross?: string;
  net_pay?: { amount: string | null; source: string };
  earnings?: { rows: SalaryRow[]; total: string };
  allowances?: { rows: SalaryRow[]; total: string };
}

export interface StructureDetail {
  code: string;
  name: string;
  version: number;
  components: StructureComponent[];
  employees: number;
  departments: number;
}

export function listEmployees(
  token: string,
  q: { search: string; department: string; location: string; structure: string; page: number },
  pageSize = 20,
) {
  const query = new URLSearchParams({ page: String(q.page), page_size: String(pageSize) });
  if (q.search.trim()) query.set("search", q.search.trim());
  if (q.department) query.set("department", q.department);
  if (q.location) query.set("location", q.location);
  if (q.structure) query.set("structure", q.structure);
  return request<DirectoryPage>(`/employees?${query}`, {}, token);
}

export const directoryFilters = (token: string) =>
  request<DirectoryFilters>("/employees/filters", {}, token);

export function salaryDetails(token: string, code: string, month?: string) {
  const query = month ? `?month=${encodeURIComponent(month)}` : "";
  return request<SalaryDetails>(`/employees/${encodeURIComponent(code)}/salary${query}`, {}, token);
}

export const structureDetail = (token: string, code: string) =>
  request<StructureDetail>(`/structures/${encodeURIComponent(code)}`, {}, token);

// ---------------------------------------------------------------- overview

export interface TrendPoint {
  period: string; label: string; gross: string; deductions: string; net: string;
  employees?: number; lop_days?: string;
}

export interface Overview {
  scope: "company" | "personal";
  headcount?: { active: number; notice: number; separated: number };
  departments?: { name: string; count: number }[];
  latest_payroll?: TrendPoint | null;
  statutory?: Record<"PF" | "ESIC" | "PT" | "TDS", string>;
  trend: TrendPoint[];
  latest_batch?: { batch_id: string; period: string; status: string; stage: string | null; total: number } | null;
  activity?: { at: string; action: string; actor: string; entity: string | null; entity_id: string | null }[];
  team_size?: number | null;
  pending_actions?: number;
}

export const getOverview = (token: string) => request<Overview>("/overview", {}, token);

// ---------------------------------------------------------------- attendance + structures

export interface AttendanceDay { date: string; status: string; remarks: string | null; lop_days: string }

export interface AttendanceDetails {
  employee_code: string; full_name: string; month: string; month_label: string;
  months: { min: string; max: string };
  available: boolean;
  message?: string;
  summary?: {
    calendar_days: number; working_days: string; week_offs: string; holidays: string;
    present_days: string; paid_leave_days: string; absent_days: string; lop_days: string;
    paid_days: string; basis_days: string; basis: string; unapproved_absences: string;
  };
  paid_days_steps?: { label: string; formula: string | null; result: string | null }[];
  paid_days_formula?: string;
  lop?: { days: string; reason: string; updated_by: string | null; updated_at: string | null } | null;
  warnings?: string[];
  payroll?: { status: string; paid_days: string; lop_days: string; version: number } | null;
  days?: AttendanceDay[];
}

export function attendanceDetails(token: string, code: string, month?: string) {
  const query = month ? `?month=${encodeURIComponent(month)}` : "";
  return request<AttendanceDetails>(`/employees/${encodeURIComponent(code)}/attendance${query}`, {}, token);
}

export interface StructureSummary {
  code: string; name: string; version: number; components: number; employees: number; departments: number;
}

export interface StructureComponent {
  head_code: string; name: string; basis: string; calc_type?: string; tab: string | null;
  tax_treatment?: string; tax_note?: string | null; pf_wage?: boolean; esic_wage?: boolean;
}

export interface StructurePreview {
  code: string; monthly_gross: string; annual_gross: string; formula: string;
  lines: { head_code: string; name: string; basis: string; amount: string }[];
  total: string; warnings: string[];
}

export const listStructures = (token: string) =>
  request<{ items: StructureSummary[]; custom_employees: number }>("/structures", {}, token);

export function structureEmployees(token: string, code: string, page: number) {
  return request<{
    total: number; page: number; pages: number;
    items: { employee_code: string; full_name: string; department: string | null;
             monthly_gross: string | null; effective_from: string | null }[];
  }>(`/structures/${encodeURIComponent(code)}/employees?page=${page}&page_size=10`, {}, token);
}

export const structurePreview = (token: string, code: string, gross: string) =>
  request<StructurePreview>(
    `/structures/${encodeURIComponent(code)}/preview?gross=${encodeURIComponent(gross)}`, {}, token);

// ---------------------------------------------------------------- overtime + one-off pay

export interface PayExtras {
  month: string; month_label?: string; restricted: boolean; message?: string;
  policy?: { version: number; method: string; enabled: boolean; max_hours_per_day: string; max_hours_per_month: string };
  rate?: { rate_per_hour: string; source: string; formula: string;
           steps: { label: string; formula: string | null; result: string | null }[] } | null;
  rate_error?: string | null;
  hours?: string; amount?: string;
  overtime?: { id: number; work_date: string; hours: string; rate_per_hour: string; rate_source: string;
               amount: string; formula: string }[];
  adjustments?: { id: number; kind: string; head_code: string; label: string | null; amount: string; reason: string }[];
}

export function payExtras(token: string, code: string, month?: string) {
  const query = month ? `?month=${encodeURIComponent(month)}` : "";
  return request<PayExtras>(`/employees/${encodeURIComponent(code)}/overtime${query}`, {}, token);
}

// ---------------------------------------------------------------- payroll runs (HR)

export interface PayrollMonth {
  period: string; label: string; employees: number; locked: number;
  gross: string; deductions: string; net: string; status: string;
  batches?: number; latest_batch_id?: string | null;
}

export interface RunBatch extends BatchStatus {
  created_at: string | null; created_by: string | null;
  approved_by: string | null; finalized_by: string | null;
}

export interface MonthDetail extends Omit<PayrollMonth, "batches"> { batches: RunBatch[] }

export interface PrecheckIssue { severity: string; employee_code: string; employee_name: string; message: string }

export interface Precheck {
  key: string; label: string; status: "PASS" | "WARNING" | "FAIL";
  passed: number; warnings: number; failed: number; issue_count: number;
  notes: { severity: string; message: string }[]; issues: PrecheckIssue[];
}

export interface Prechecks {
  period: string; label: string; filters: Record<string, string>; employees: number;
  already_processed: number; checks: Precheck[];
  summary: { passed: number; warnings: number; failed: number };
  blocking: string[]; can_run: boolean;
}

export interface ValidationPage {
  stage: string | null; total: number; page: number;
  counts: { errors: number; warnings: number; passed: number };
  items: { severity: string; code: string; message: string; employee_code: string | null; employee_name: string | null }[];
}

export interface Challan {
  period: string; contributing_members: number; admin_minimum_applied: boolean;
  accounts: { account: string; name: string; amount: string }[]; total: string;
}

const enc = encodeURIComponent;

export const payrollMonths = (token: string) => request<PayrollMonth[]>("/payroll-runs", {}, token);
export const payrollMonth = (token: string, month: string) =>
  request<MonthDetail>(`/payroll-runs/${enc(month)}`, {}, token);

export function payrollPrechecks(token: string, month: string, department: string, workState: string) {
  const q = new URLSearchParams();
  if (department) q.set("department", department);
  if (workState.trim()) q.set("work_state", workState.trim());
  return request<Prechecks>(`/payroll-runs/${enc(month)}/prechecks?${q}`, {}, token);
}

export const startBatch = (token: string, month: string, department: string, workState: string) =>
  request<BatchStatus>("/batches", {
    method: "POST",
    body: JSON.stringify({ month, department: department || null, work_state: workState.trim() || null }),
  }, token);

export const validateBatch = (token: string, id: string) =>
  request<{ errors: number; warnings: number; passed: number; stage: string }>(
    `/batches/${enc(id)}/validate`, { method: "POST" }, token);

export function validationResults(token: string, id: string, severity: string, page: number) {
  const q = new URLSearchParams({ page: String(page), page_size: "20" });
  if (severity) q.set("severity", severity);
  return request<ValidationPage>(`/batches/${enc(id)}/validation?${q}`, {}, token);
}

export const approveBatch = (token: string, id: string) =>
  request<BatchStatus>(`/batches/${enc(id)}/approve`, { method: "POST" }, token);
export const finalizeBatch = (token: string, id: string) =>
  request<BatchStatus>(`/batches/${enc(id)}/finalize`, { method: "POST" }, token);
export const rollbackBatch = (token: string, id: string, reason: string) =>
  request<BatchStatus>(`/batches/${enc(id)}/rollback`, { method: "POST", body: JSON.stringify({ reason }) }, token);
export const pfChallan = (token: string, month: string) =>
  request<Challan>(`/payroll-runs/${enc(month)}/challan`, {}, token);

// ---------------------------------------------------------------- payroll changes (HR)

export interface PayrollChange {
  lop_days?: string | null;
  overtime?: { work_date: string; hours: string }[];
  adjustments?: { kind: "EARNING" | "DEDUCTION"; head_code: string; amount: string; label?: string | null }[];
  salary?: { monthly_gross: string; effective_from: string; template_code?: string | null } | null;
  attendance?: { date: string; status: string; remarks?: string | null }[];
  fill_month?: boolean;
  reason: string;
}

export interface ChangeRow { key: string; label: string; unit?: string; before: string; after: string; difference: string }

export interface ChangePreview {
  employee_code: string; employee_name: string; period: string; period_label: string;
  rows: ChangeRow[]; net_change: string; warnings: string[]; saved: boolean; preview_id: string;
  overtime: { work_date: string; formula: string; rate_source: string }[];
  recalculated?: string[]; not_recalculated?: string[];
  requires_approval?: boolean; no_attendance_before?: boolean;
}

export interface ApprovalSetting { change_approval: boolean; approvers: { name: string; email: string }[] }

export const approvalSetting = (token: string) => request<ApprovalSetting>("/actions/settings", {}, token);
export const setApprovalSetting = (token: string, enabled: boolean, reason: string) =>
  request<ApprovalSetting>("/actions/settings", {
    method: "PUT", body: JSON.stringify({ enabled, reason }) }, token);

export const simulateChange = (token: string, employeeCode: string, month: string, change: PayrollChange) =>
  request<ChangePreview>("/payroll/simulate", {
    method: "POST", body: JSON.stringify({ employee_code: employeeCode, month, change }),
  }, token);

/** Submit a confirmed preview: applied at once, or sent for a second HR user's approval. */
export const applyChange = (token: string, employeeCode: string, month: string, change: PayrollChange,
  previewId: string, source: "ui" | "chat" = "ui") =>
  request<ActionItem>("/payroll/apply", {
    method: "POST", body: JSON.stringify({ employee_code: employeeCode, month, change, preview_id: previewId, source }),
  }, token);

export const cancelEntry = (token: string, kind: "overtime" | "adjustment", id: number, reason: string) =>
  request<{ recalculated: string[] }>(`/payroll/entries/${kind}/${id}/cancel`, {
    method: "POST", body: JSON.stringify({ reason }),
  }, token);

export const recalculateMonth = (token: string, month: string, reason: string) =>
  request<BatchStatus>(`/payroll-runs/${encodeURIComponent(month)}/recalculate`, {
    method: "POST", body: JSON.stringify({ reason }),
  }, token);

// ---------------------------------------------------------------- action center (HR)

export type ActionStatus = "PENDING_APPROVAL" | "APPLIED" | "REJECTED" | "FAILED" | "CANCELLED";

export interface ActionItem {
  id: string; status: ActionStatus; kind: string; kind_label: string; title: string;
  employee_code: string; employee_name: string; period: string; period_label: string;
  reason: string; source: string; change: PayrollChange;
  preview: { rows: ChangeRow[]; net_change: string; warnings: string[] };
  net_change: string; requested_by: string | null; requested_by_code: string; requested_at: string | null;
  decided_by: string | null; decided_at: string | null; decision_note: string | null;
  applied_at: string | null; result: { recalculated: string[]; net_change: string; undo?: Record<string, unknown> | null; undo_note?: string; undone_by?: string } | null;
  error: string | null; message?: string;
}

export interface ActionPage {
  total: number; page: number; pages: number;
  counts: Record<ActionStatus, number>; items: ActionItem[];
}

export function listActions(token: string, status: string, page: number, search = "") {
  const q = new URLSearchParams({ page: String(page), page_size: "20" });
  if (status) q.set("status", status);
  if (search.trim()) q.set("search", search.trim());
  return request<ActionPage>(`/actions?${q}`, {}, token);
}

export const actionPreview = (token: string, id: string) =>
  request<ChangePreview>(`/actions/${encodeURIComponent(id)}/preview`, {}, token);
export const approveAction = (token: string, id: string, previewId: string) =>
  request<ActionItem>(`/actions/${encodeURIComponent(id)}/approve`, {
    method: "POST", body: JSON.stringify({ preview_id: previewId }) }, token);
export const rejectAction = (token: string, id: string, note: string) =>
  request<ActionItem>(`/actions/${encodeURIComponent(id)}/reject`, {
    method: "POST", body: JSON.stringify({ note }) }, token);

// ---------------------------------------------------------------- AI audit + compliance (HR)

export interface AuditIssue {
  employee_code: string; employee_name: string; issue: string; severity: "HIGH" | "MEDIUM" | "LOW";
  category: string; type?: string; title?: string; component?: string; unit?: string;
  expected?: string | null; actual?: string | null; difference?: string | null;
  causes?: string[]; recommendation?: string; effects?: string[]; can_fix?: boolean; message?: string;
}

export interface AuditHealth {
  score: number;
  categories: { category: string; status: "PASS" | "WARNING" | "FAIL"; passed: number; warnings: number; failed: number }[];
  employees: { passed: number; warnings: number; failed: number };
}

export interface AuditRun {
  id: string; period: string; period_label: string; filters: Record<string, string>;
  status: "RUNNING" | "COMPLETED" | "FAILED"; total: number; checked: number;
  created_by: string | null; started_at: string | null; finished_at: string | null;
  summary: {
    employees: number; payslips_checked: number; passed: number; warnings: number; failed: number;
    findings: number; anomalies: number; by_issue: Record<string, number>;
    statutory_impact: Record<string, string>; net_overpaid: string; net_underpaid: string; note: string;
  } | null;
  health: AuditHealth | null; error: string | null;
  findings?: AuditIssue[]; anomalies?: AuditIssue[];
}

export const startAudit = (token: string, month: string) =>
  request<AuditRun>("/audits", { method: "POST", body: JSON.stringify({ month }) }, token);
export const auditRun = (token: string, id: string) => request<AuditRun>(`/audits/${encodeURIComponent(id)}`, {}, token);
export const auditRuns = (token: string) => request<AuditRun[]>("/audits", {}, token);
export const recalculateEmployee = (token: string, month: string, code: string, reason: string) =>
  request<{ old_net: string; new_net: string }>(
    `/payroll-runs/${encodeURIComponent(month)}/employees/${encodeURIComponent(code)}/recalculate`,
    { method: "POST", body: JSON.stringify({ reason }) }, token);

export interface RuleStatus { configured: boolean; rule?: string; status: string; effective_from?: string; levied?: boolean; detail?: string }

export interface Compliance {
  period: string; label: string; employees: number;
  pf: { versions: { version: string; from: string; to: string; employee_rate: string; employer_rate: string; eps_rate: string; pf_ceiling: string; status: string }[]; error: string | null; split_month: boolean };
  esic: { rule: RuleStatus | null; employees: number };
  states: { state: string; employees: number; pt: RuleStatus; lwf: RuleStatus }[];
  tds: (RuleStatus & { regime: string; employees: number })[];
  challan: Challan | null;
  audit: { id: string; score: number; finished_at: string; statutory: AuditHealth["categories"] | null } | null;
  unverified: string[];
}

export const compliance = (token: string, month: string) =>
  request<Compliance>(`/payroll-runs/${encodeURIComponent(month)}/compliance`, {}, token);

// ---------------------------------------------------------------- knowledge base

export interface KbPassage {
  title: string; section: string; snippet: string; source: string; status: string;
  jurisdiction?: string; effective_from?: string | null; effective_to?: string | null; document_id: number;
  policy_type?: string; document_type?: string; score?: number;
}

export interface KbDocument {
  id: number; title: string; path: string; policy_type: string; document_type: string;
  version: string | null; jurisdiction: string; status: string; source: string;
  effective_from: string | null; effective_to: string | null; access_roles: string[];
  chunks: number; uploaded: boolean; generated: boolean; content?: string;
}

export const kbSearch = (token: string, q: string) =>
  request<{ query: string; results: KbPassage[]; answer: { found: boolean; text: string } }>(
    `/knowledge/search?q=${encodeURIComponent(q)}`, {}, token);
export const kbDocuments = (token: string) =>
  request<{ documents: KbDocument[]; stats: { documents: number; chunks: number; unverified: number } }>("/knowledge/documents", {}, token);
export const kbDocument = (token: string, id: number) => request<KbDocument>(`/knowledge/documents/${id}`, {}, token);
export const kbUpload = (token: string, body: {
  filename: string; content: string; title: string; policy_type: string; document_type: string;
  source: string; access_roles: string[]; effective_from?: string | null;
}) => request<KbDocument>("/knowledge/documents", { method: "POST", body: JSON.stringify(body) }, token);
export const kbRemove = (token: string, id: number, reason: string) =>
  request<{ removed: number }>(`/knowledge/documents/${id}/remove`, { method: "POST", body: JSON.stringify({ reason }) }, token);
export const kbReindex = (token: string) =>
  request<{ added: number; updated: number; unchanged: number; removed: number; errors: string[] }>("/knowledge/reindex", { method: "POST" }, token);

// ---------------------------------------------------------------- payroll run register / repair (HR)

export interface RegisterRow {
  employee_code: string; full_name: string; department: string | null;
  status: "PROCESSED" | "LOCKED" | "FAILED" | "NOT_PROCESSED";
  attendance_days: number; undecided_absences: string; lop_days: string; paid_days: string | null; gross: string | null;
  deductions: string | null; net: string | null; one_off: number; overtime: number; error: string | null;
}

export function monthEmployees(token: string, month: string, search: string, page: number) {
  const q = new URLSearchParams({ page: String(page), page_size: "15" });
  if (search.trim()) q.set("search", search.trim());
  return request<{ total: number; page: number; pages: number; items: RegisterRow[] }>(
    `/payroll-runs/${encodeURIComponent(month)}/employees?${q}`, {}, token);
}

export const createMonthAttendance = (token: string, month: string, reason: string) =>
  request<{ employees: number; days: number; up_to: string }>(
    `/payroll-runs/${encodeURIComponent(month)}/attendance/create`, { method: "POST", body: JSON.stringify({ reason }) }, token);

export const runSelected = (token: string, month: string, codes: string[], reason: string,
  lopDays: Record<string, string> = {}) =>
  request<BatchStatus>(`/payroll-runs/${encodeURIComponent(month)}/run-selected`, {
    method: "POST", body: JSON.stringify({ employee_codes: codes, reason, lop_days: lopDays }) }, token);

export const reopenMonth = (token: string, month: string, reason: string) =>
  request<{ unlocked: number; batches: number }>(
    `/payroll-runs/${encodeURIComponent(month)}/reopen`, { method: "POST", body: JSON.stringify({ reason }) }, token);

// ---------------------------------------------------------------- audit timeline + settings

export interface TimelineEvent {
  id: number; at: string | null; action: string; label: string; group: string;
  actor: string; actor_role: string | null; entity: string | null; entity_id: string | null;
  employee: string | null; summary: string; reason: string | null;
  old: Record<string, unknown> | null; new: Record<string, unknown> | null; request_id: string | null;
}

export function auditTimeline(token: string, f: { employee?: string; action?: string; dateFrom?: string; dateTo?: string; system?: boolean; page: number }) {
  const q = new URLSearchParams({ page: String(f.page), page_size: "30" });
  if (f.employee?.trim()) q.set("employee", f.employee.trim());
  if (f.action) q.set("action", f.action);
  if (f.dateFrom) q.set("date_from", f.dateFrom);
  if (f.dateTo) q.set("date_to", f.dateTo);
  if (f.system) q.set("include_system", "true");
  return request<{ total: number; page: number; pages: number; items: TimelineEvent[]; groups: string[] }>(`/audit/timeline?${q}`, {}, token);
}

export interface UserRow {
  employee_code: string; full_name: string; email: string; department: string | null;
  role: string; role_label: string; login_enabled: boolean; permissions: string[];
}

export const listUsers = (token: string, search = "") =>
  request<{ users: UserRow[]; roles: { role: string; label: string; permissions: string[] }[] }>(
    `/settings/users${search.trim() ? `?search=${encodeURIComponent(search.trim())}` : ""}`, {}, token);
export const updateUser = (token: string, code: string, body: { role?: string; login_enabled?: boolean; reason: string }) =>
  request<{ role: string; role_label: string; login_enabled: boolean }>(`/settings/users/${encodeURIComponent(code)}`, {
    method: "PUT", body: JSON.stringify(body) }, token);
export const companySettings = (token: string) =>
  request<{ company_name: string; payroll: Record<string, unknown>; overtime_policy: Record<string, unknown>;
    leave_entitlements: Record<string, string>; llm: { provider: string; model: string | null } }>("/settings/company", {}, token);

// ---------------------------------------------------------------- reports + undo

export interface ReportData {
  key: string; title: string; period: string;
  columns: { key: string; label: string; kind: "text" | "money" | "number" | "days" }[];
  rows: Record<string, string | number | null>[];
  totals: Record<string, string | number>; notes: string[];
}

export const listReports = (token: string) => request<{ key: string; title: string }[]>("/reports", {}, token);

function reportQuery(month: string, department: string, format: string) {
  const q = new URLSearchParams({ month, format });
  if (department) q.set("department", department);
  return q;
}

export const getReport = (token: string, key: string, month: string, department: string) =>
  request<ReportData>(`/reports/${encodeURIComponent(key)}?${reportQuery(month, department, "json")}`, {}, token);

export const downloadReport = (token: string, key: string, month: string, department: string, format: "csv" | "xlsx" | "pdf") =>
  downloadFile(token, `/reports/${encodeURIComponent(key)}?${reportQuery(month, department, format)}`, `${key}_${month}.${format}`);

export const undoPreview = (token: string, id: string) =>
  request<ChangePreview>(`/actions/${encodeURIComponent(id)}/undo-preview`, {}, token);
export const undoAction = (token: string, id: string, previewId: string, reason: string) =>
  request<ActionItem>(`/actions/${encodeURIComponent(id)}/undo`, {
    method: "POST", body: JSON.stringify({ preview_id: previewId, reason }) }, token);

// ---------------------------------------------------------------- leave + tax help

export interface LeaveRow {
  id: number; employee_code: string | null; employee_name: string | null; leave_type: string; leave_label: string;
  from: string; to: string; days: string; reason: string | null; status: "PENDING" | "APPROVED" | "REJECTED" | "CANCELLED";
  applied_at: string | null; decided_at: string | null;
}
export interface LeavePlan {
  leave_type: string; leave_label: string; from: string; to: string; days: string; dates: string[];
  skipped: { date: string; why: string }[]; balance: { available: string; after: string } | null;
  warnings: string[]; approvers: string[];
}
export interface LeaveForm { leave_type: string; from: string; to: string; reason: string }

const leaveBody = (f: LeaveForm) => JSON.stringify({ leave_type: f.leave_type, from_date: f.from, to_date: f.to || f.from, reason: f.reason });

export const myLeaves = (token: string) =>
  request<{ financial_year: string; approvers: string[]; requests: LeaveRow[];
    balances: { leave_type: string; label: string; entitlement: string; taken: string; pending: string; available: string }[] }>("/leaves", {}, token);
export const previewLeave = (token: string, f: LeaveForm) =>
  request<LeavePlan>("/leaves/preview", { method: "POST", body: leaveBody(f) }, token);
export const applyLeave = (token: string, f: LeaveForm) =>
  request<LeaveRow & { approvers: string[] }>("/leaves", { method: "POST", body: leaveBody(f) }, token);
export const cancelLeave = (token: string, id: number) =>
  request<LeaveRow>(`/leaves/${id}/cancel`, { method: "POST" }, token);
export const leavesToApprove = (token: string) => request<LeaveRow[]>("/leaves/to-approve", {}, token);
export const decideLeave = (token: string, id: number, approve: boolean, note: string) =>
  request<LeaveRow & { recalculated?: string[] }>(`/leaves/${id}/${approve ? "approve" : "reject"}`, {
    method: "POST", body: JSON.stringify({ note }) }, token);
export function taxCompare(token: string, declared: Record<string, string>, employee?: string) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(declared)) if (Number(v) > 0) q.set(k, v);
  if (employee) q.set("employee", employee);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- same payload as the chat tax card
  return request<Record<string, any>>(`/tax/compare?${q}`, {}, token);
}
