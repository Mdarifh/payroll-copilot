# Payroll Chatbot

A payroll chatbot for an Indian company. Employees and HR ask payroll questions and
perform payroll actions through chat. An LLM - free local **Ollama** or **Claude**
(Anthropic API), switchable in `.env` - chooses **tools**; every number comes from the
deterministic payroll engine in Python, never from the LLM.

> **Status: Phase 3 of 5 done** - React chat screen with login, cards and quick actions.
>
> Data: local SQLite `data/chatbot.db` with fictional demo data only.
> The previous "AI Payroll Copilot" (Streamlit + QA agent) is kept on the git branch
> `legacy/payroll-copilot`.

| Phase | Scope | Status |
|---|---|---|
| 1 | Database schema + seed data + login | ✅ done |
| 2 | Chat backend with LLM tool calling (read-only tools) | ✅ done |
| 3 | React + Tailwind chat UI with cards | ✅ done |
| 4 | Action tools with confirmation, LOP flow, audit log | next |
| 5 | RAG over policy documents | |

## Open the app (easiest)

Double-click **`Start Payroll Chatbot.bat`** in the project folder. It starts Ollama (if
needed), the API and the chat screen in their own windows, then opens
**http://127.0.0.1:5173**. Keep the "Payroll API" and "Payroll Chat UI" windows open.
Sign in with a demo login (click one under "Demo logins" to fill the form).

The first answer can take up to a minute while the model loads into memory.

## Setup (Windows PowerShell)

Requires [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically).

```powershell
cd "D:\Payroll QA Agent"
uv sync                          # create .venv and install dependencies
copy .env.example .env           # then set JWT_SECRET (see below)
.\.venv\Scripts\Activate.ps1
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as JWT_SECRET in .env
```

## Run Phase 1

```powershell
payroll-chatbot db seed          # create tables, load statutory rules, add demo data
payroll-chatbot db status        # row count per table
payroll-chatbot api serve        # http://127.0.0.1:8000/docs
```

Then in the browser open **http://127.0.0.1:8000/docs**:

1. `POST /auth/login` → *Try it out* →
   `{"email": "rahul.verma@example.com", "password": "Employee@123"}` → copy `access_token`.
2. Click **Authorize** (top right) → enter the email as *username* and the password →
   *Authorize*. Swagger now sends the token on every call.
3. `GET /auth/me` → your profile. `GET /hr/employees` → **403** for an employee.
4. *Logout* in Authorize, log in as HR (`anjali.mehta@example.com` / `Hr@12345`) →
   `GET /hr/employees` → **200** with all 6 people.

Other commands: `payroll-chatbot db reset --yes` (delete the SQLite DB and seed again),
`payroll-chatbot rules load`, `payroll-chatbot check-config`, `pytest`, `ruff check .`.

## Run Phase 2 (chat)

Pick the model in `.env`:

| `LLM_PROVIDER` | Needs | Cost |
|---|---|---|
| `ollama` (default) | [Ollama](https://ollama.com) running + `ollama pull qwen2.5:7b` (~4.7 GB download, ~6 GB RAM while answering) | free, local |
| `anthropic` | `ANTHROPIC_API_KEY` from console.anthropic.com (`ANTHROPIC_MODEL`, default `claude-sonnet-5`) | billed per use - a claude.ai / Claude Code subscription login does not work for apps |
| `none` | - | chat replies that the assistant is off |

```powershell
payroll-chatbot llm check                          # is the model reachable?
payroll-chatbot chat rahul.verma@example.com       # chat in the terminal (asks the password)
payroll-chatbot chat anjali.mehta@example.com      # as HR
payroll-chatbot api serve                          # then POST /chat in http://127.0.0.1:8000/docs
```

Try: *"Mera August ka payslip dikhao"*, *"How many leaves do I have left?"*,
*"Mera is saal ka tax kitna banega?"*, as HR: *"EMP-102 ke September ke absents dikhao"*.
As an employee, *"Show Priya's payslip"* cannot work: employee tools have no employee
parameter at all.

### Chat API

`POST /chat` `{"message": "...", "conversation_id": "optional"}` (logged in) →

```json
{"conversation_id": "a1b2c3d4e5f6a7b8",
 "messages": [{"type": "text", "text": "Your August net pay was ₹58,000."},
              {"type": "payslip_card", "payslip": {"...": "..."},
               "download_url": "/payslips/2026-08/pdf"}],
 "tools_used": ["get_payslip"], "model": "ollama:qwen2.5:7b", "warnings": []}
```

Other endpoints: `GET /chat/tools` (tools for your role), `DELETE /chat/{id}` (forget a
conversation), `GET /payslips/{YYYY-MM}` (your own processed payslip). If the model is
not reachable, `/chat` answers **503** with what to do.

### Tools (read-only)

| Tool | Role | Returns |
|---|---|---|
| `get_payslip(month?)` | employee, hr | own processed payslip (+ `payslip_card`); latest if no month |
| `get_leave_balance()` | employee, hr | CL / SL / EL entitlement, taken, pending, available (current FY) |
| `get_tax_summary(financial_year?)` | employee, hr | projected salary, deductions, taxable income, annual tax, TDS so far, monthly TDS |
| `get_attendance_exceptions(emp_id, month)` | **hr** | absences without approved leave, LOP decided, approved leaves, payroll status |

How the chat stays safe and correct (`src/payroll_copilot/chat/`):

- The model only sees the tools for the user's role, and `run_tool` checks the role again.
- Employee tools act on the logged-in user (no employee parameter); tool arguments are
  validated (strict schemas, `additionalProperties: false`).
- Cards are built from tool output, not from model text.
- If the model writes a number that no tool returned, its text is replaced by the tool
  facts and a warning is returned (`ungrounded_numbers`).
- At most `LLM_MAX_STEPS` model calls per message; refusals and cut-off answers are handled.
- Every tool call (`chat.tool`, incl. denied ones) and message (`chat.ask`) is audited;
  tool audit rows are written at the end of the turn so no DB write lock is held while
  the model thinks.
- Conversation memory is in-memory per user (last 10 turns; lost on API restart).
- Claude: official `anthropic` SDK, manual tool-use loop; the full assistant content is
  sent back each step; no sampling parameters (current models reject them).

Payroll for August 2026 is seeded as already processed; September is not (HR runs it in
Phase 4), so *"September payslip"* correctly answers that it is not processed yet.
Unapproved absences are never turned into LOP automatically - the payslip warns until HR
decides.

## Chat screen (Phase 3)

`frontend/` - React 19 + TypeScript + Tailwind CSS 4, built with Vite.

```powershell
cd frontend
npm install        # first time only
npm run dev        # http://127.0.0.1:5173 (calls the API through the /api proxy)
npm run build      # type-check + production build
```

- Login page (with one-click demo logins); session kept in `sessionStorage` (ends when the
  tab closes); an expired token sends you back to login.
- Header: bot name and the logged-in user with a lock icon (and an HR badge).
- Messages rendered by `type`: `text` bubbles and `payslip_card` (earnings, deductions,
  net pay, **Download PDF** -> `GET /payslips/{month}/pdf`, audited). `confirm_card` and
  `lop_card` arrive with Phase 4.
- Quick action chips: Payslip, Leave balance, Tax summary, Form 16; HR also gets Edit LOP
  days and Run payroll (these two answer properly once Phase 4 adds the action tools).
- ₹ amounts with Indian digit grouping (`Intl.NumberFormat("en-IN")`), "thinking"
  indicator, clear errors when the API or the model is down, New chat, Logout.

## Bulk payroll (any number of employees)

HR: *"September 2026 ka payroll sab employees ka process karo"* (or *"sirf Sales team"*,
*"sirf Delhi office"*) -> the LLM calls `start_payroll_review(month, department?,
work_state?)` and gets back **only counts** (total, ok, needs review, batch_id); the full
employee list never passes through the LLM. The chat shows a **bulk LOP card**:

- tiles (total / no issues / needs LOP review), searchable paged table (checkbox, name + ID,
  absent dates, −/+ LOP days in 0.5 steps up to the absent days, live ₹ deduction), the total
  deduction for the **whole** batch from the backend, a required reason, and the buttons
  **Sab skip karo** / **LOP lagao aur payroll chalao**;
- then a progress bar (polls every 2 s), a summary, **Download report (CSV)** (processed,
  LOP applied, failed + error per employee) and **Retry failed only**.

How it works (`services/batch_service.py`, `services/batch_worker.py`):

| Step | What happens |
|---|---|
| review | employees read from **this app's database** in pages of 100 through `LocalPayrollSource` (rate limiter + retry/backoff; no external or Zimyo system); every employee becomes a `batch_items` row (the job queue), employees with absences without approved leave become `batch_lop_items` (dates, suggested days, gross, divisor, per-day rate) |
| run | `POST /batches/{id}/run`: one conditional `UPDATE ... WHERE status='REVIEW'` - a double click / second request gets **409** and nothing runs twice; LOP written to `lop_records` for checked rows, audited per employee with the `batch_id` |
| worker | background thread inside the API (a DB-backed queue - no Redis/Celery needed on Windows); chunks of 50; **each employee in its own transaction** (payroll run + item status + audit commit together), so a crash/restart never pays anyone twice and the worker resumes `RUNNING` batches on start; one employee's error is recorded and the batch continues; temporary errors retried 3× with backoff; employee-months already processed are skipped |

LOP per day = `LOP_BASE / LOP_DIVISOR` from `.env` (default gross / calendar days) and must
match the payroll engine's proration (`DEFAULT_PAID_DAYS_BASIS`); `check-config` reports a
mismatch, and a batch refuses to start with one. `LOP_BASE=BASIC` is not supported by the
engine yet.

Endpoints (HR only): `POST /batches`, `GET /batches/{id}/status`, `GET
/batches/{id}/lop-items?page=&search=`, `PATCH /batches/{id}/lop-items/{emp_id}`,
`POST /batches/{id}/run`, `POST /batches/{id}/retry-failed`, `GET /batches/{id}/report.csv`.
Settings: `BATCH_PAGE_SIZE`, `BATCH_SOURCE_RATE`, `BATCH_CHUNK_SIZE`, `BATCH_MAX_ATTEMPTS`,
`BATCH_WORKER_ENABLED`.

Tests (`tests/unit/batch/`): 5,000 employees with 2% exceptions, one employee failing +
retry failed only, 5 simultaneous run clicks (exactly one runs), department / state filters,
server killed mid-batch then restarted (everyone processed exactly once), already processed
employees skipped, temporary errors retried, end-to-end with the real payroll engine.

## Demo users

| Code | Name | Login | Role | Scenario |
|---|---|---|---|---|
| EMP-101 | Rahul Verma | rahul.verma@example.com / Employee@123 | employee | MH, ₹60,000; EL in Aug; 1 unapproved absence in Sep |
| EMP-102 | Priya Nair | priya.nair@example.com / Employee@123 | employee | KA, ₹40,000; approved CL + 2 unapproved absences in Sep |
| EMP-103 | Rohan Das | rohan.das@example.com / Employee@123 | employee | WB, ₹18,000, ESIC; approved SL; pending EL for Oct |
| EMP-104 | Meena Iyer | meena.iyer@example.com / Employee@123 | employee | TN, ₹50,000, old tax regime; clean month |
| EMP-105 | Kavita Reddy | kavita.reddy@example.com / Employee@123 | employee | TG, ₹25,000; joined 15-Sep-2026 |
| HR-001 | Anjali Mehta | anjali.mehta@example.com / Hr@12345 | **hr** | MH, ₹75,000 |

Plus **50 generated employees** `EMP-201` … `EMP-250` (`dummy_employees: 50` in the seed
file): repeatable fake names, 7 departments, 9 states, gross ₹15,000–₹2,00,000 (ESIC up to
₹21,000), ~20% with unapproved absences in September, August payroll processed. Login
`<firstname>.<lastname>.<number>@example.com` / `Employee@123` (HR sees the list in
`GET /hr/employees`). Add more to an existing DB: `payroll-chatbot db add-dummy --count 50`.

Seed file: `config/seed/chatbot_seed.yaml`. Attendance is generated per day for Aug and
Sep 2026: Sundays = week off, 15-Aug and 14-Sep = holidays, approved leaves = LEAVE,
listed absences = ABSENT (an ABSENT day without approved leave is an *attendance
exception* for the Phase 4 LOP flow), everything else = PRESENT.

## Database

| Table | Purpose |
|---|---|
| `employees` | master data + login (email, bcrypt `password_hash`, `role` employee/hr, `login_enabled`) and PF/ESIC/PT/tax flags |
| `salary_structure` | full-month amount per salary head, per effective date (split from gross by the structure engine) |
| `attendance` | one row per employee per day (PRESENT / ABSENT / LEAVE / HALF_DAY / WEEK_OFF / HOLIDAY) |
| `leaves` | leave applications (CL / SL / EL / LOP, PENDING / APPROVED / ...) |
| `lop_records` | LOP days HR decided per employee and month, with reason |
| `payroll_runs` | processed payroll per employee and month, full payslip JSON |
| `audit_log` | who (`actor_id`, `actor_role`), what (`action`, `entity`), when (`at`), `old_value`, `new_value`, `request_id` |
| `pf_rules`, `esic_rules`, `pt_rules`/`pt_slabs`, `lwf_rules`, `tds_rules`/`tds_slabs` | versioned statutory rules from `config/rules/**` |

Migrations: `alembic/versions` (SQLite now; the models are PostgreSQL-ready).

## Login and security

- `POST /auth/login` (JSON) or `POST /auth/token` (form, used by Swagger) →
  JWT (HS256, `JWT_EXPIRE_MINUTES`, default 8 h) with the employee id and role.
- Every request reloads the employee: a disabled login or changed role stops old tokens
  immediately. A token whose role does not match the database is rejected.
- Role checks are FastAPI dependencies in the backend (`require_hr`), never only a prompt.
- Wrong email and wrong password give the same message and take the same time.
- Passwords: bcrypt; longer than 72 bytes is rejected (bcrypt would silently truncate).
- Every login and failed login is written to `audit_log` (never the password).
- `JWT_SECRET` is required in prod (≥ 32 characters). Without it in dev a random key is
  used per run. The API refuses a non-localhost host without `JWT_SECRET`.

## Payroll engine (kept from the previous project)

`src/payroll_copilot/calculations` + `config/rules`: salary proration, PF/EPS/EDLI,
ESIC, PT (all states), LWF, TDS (old/new regime), arrears, F&F, each with a calculation
trace. All rule values are seed values marked **UNVERIFIED** - verify them before real
use. EPF: ₹15,000 ceiling until 31-Aug-2026, ₹25,000 from 01-Sep-2026.

LOP policy (used from Phase 4) comes from config, not the LLM:
`LOP per day = LOP_BASE (GROSS | BASIC) / LOP_DIVISOR (FIXED_30 | CALENDAR | WORKING_DAYS)`.

## Layout

```
config/rules/        versioned statutory rules (YAML)
config/payroll/      salary heads and structure templates
config/seed/         demo company for the chatbot
alembic/             migrations
src/payroll_copilot/
  core/          config, money (Decimal only), dates, logging, exceptions
  domain/        enums and value objects shared by the engine
  calculations/  deterministic payroll engine
  rules/         rule loader, applicability, resolver
  db/            models, session, migrations, seed
  auth/          password hashing, JWT, current user + role dependencies, login
  repositories/  DB -> engine inputs (daily attendance + LOP -> monthly record, ...)
  services/      payroll / PF / ESIC / PT / LWF / TDS services, payslip, leave, audit
  chat/          tools (role checks) and the chat loop (grounding, memory)
  llm/           providers: Ollama (HTTP) and Claude (anthropic SDK)
  api/           FastAPI app
tests/unit/      engine, rules, config, schema + seed, auth, tools, chat loop, providers
```
