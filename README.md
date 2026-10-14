# PayrollAI Copilot

An AI payroll assistant for an Indian company. HR runs payroll, fixes LOP / attendance /
one-off pay, approves changes, audits payroll and downloads reports; employees see their
payslips, apply for leave and compare tax regimes - from a web app or by chatting in
English or Hinglish.

**Every number comes from the deterministic payroll engine in Python, never from the AI.**
The AI (free local **Ollama**, or **Claude** via the Anthropic API) only words the
answer; deterministic routing handles most questions without the model at all.

> Data: local SQLite `data/chatbot.db` with fictional demo data only. The app reads and
> writes only its own database - it never connects to any external payroll system.
> The previous payroll QA agent is kept on the local git branch `legacy/payroll-copilot`.

## Features

| Area | What you can do |
|---|---|
| **Overview** | Payroll month at a glance: headcount, gross / net, pending approvals, issues |
| **AI Chat** | Ask about pay, PF, ESIC, PT, TDS, LWF, attendance and policies; compare months; preview changes before applying; apply for leave; old vs new tax regime. Chat history is saved, follow-ups ("what about EMP-102?") work |
| **Employees** | Directory with filters; salary, attendance calendar (edit days, create missing attendance), LOP, overtime, bonus / misc pay, salary revision |
| **Payroll Runs** | Prechecks, run the whole month or **selected employees** (with LOP per employee), validate, approve, finalize (lock) and reopen a month, per-employee register |
| **Salary Structures** | Templates, components and the employees on each |
| **Compliance** | PF / ESIC / PT / TDS / LWF totals and rules per month |
| **AI Audit** | Anomaly detection with health score, root causes and one-click recalculation |
| **Action Center** | Every change as a request: preview → submit → **maker-checker approval** → apply → recalculate; **Undo** of applied changes |
| **Leave** | Balances, apply (checked against balance, week offs, holidays, overlaps), withdraw; managers / HR approve → leave written into attendance, payslip recalculated |
| **Reports** | Payroll register, PF, ESIC, PT, TDS, LOP, overtime, bonus / misc, arrears, salary variance, anomalies - on screen or **CSV / Excel / PDF** |
| **Knowledge Base** | Policy documents (BM25 search with role and date filters), quoted with sources in chat |
| **Audit Timeline** | Every change with who, when, why, before and after |
| **Settings** | Users and roles (Admin), change-approval switch, company payroll settings |

### Chat examples

| Ask | Answer |
|---|---|
| *Rahul ka September PF kitna hai?* | PF with the calculation (wage, ceiling, rate) |
| *Compare EMP-101 August vs September* | Line-by-line difference with reasons |
| *EMP-101 ka 2 din LOP lagao* / *15 Sep absent mark karo* | Preview card → Review & Apply (approval if switched on) |
| *Run September 2026 payroll* | Prechecks and the run card |
| *Run AI payroll audit* | Issues by severity with fixes |
| *Sick leave 20 to 22 Oct because fever* / *kal CL chahiye* | Leave card: working days, balance before → after, Apply |
| *Which regime is better for me?* / *80C 1.5 lakh 80D 25000 se kitna tax bachega* | Old vs new regime for the FY and unused deduction limits |
| *Leave policy kya hai?* | Quoted policy passages with sources |

## Roles

| Role | Can |
|---|---|
| Admin | everything, incl. users / roles and the approval switch |
| Payroll Manager | view, edit attendance / pay, run payroll, approve, audit timeline |
| Payroll Executive | view, edit attendance / LOP / overtime, run payroll (no approval) |
| Viewer | view payroll, reports and compliance (read-only) |
| Manager | own team's profiles (no amounts), approve team leave |
| Employee | own payslips, leave, tax |

Permissions are checked in the API (FastAPI dependencies) and mirrored in the UI.
Maker-checker: nobody approves their own change or leave.

## Open the app (easiest)

Double-click **`Start Payroll Chatbot.bat`**. It starts Ollama (if needed), the API and the
web app, then opens **http://127.0.0.1:5173**. Click a demo login to fill the form.
The first AI answer can take up to a minute while the model loads.

## Setup (Windows PowerShell)

Requires [uv](https://docs.astral.sh/uv/) (installs Python 3.12) and Node.js.

```powershell
cd "D:\Payroll QA Agent"
uv sync                                    # .venv + dependencies
copy .env.example .env                     # set JWT_SECRET (below)
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as JWT_SECRET
.\.venv\Scripts\payroll-chatbot.exe db seed       # tables, statutory rules, demo data
.\.venv\Scripts\payroll-chatbot.exe api serve     # API: http://127.0.0.1:8000/docs

cd frontend
npm install                                # first time only
npm run dev                                # web app: http://127.0.0.1:5173
```

AI model (`.env`):

| `LLM_PROVIDER` | Needs | Cost |
|---|---|---|
| `ollama` (default) | [Ollama](https://ollama.com) + `ollama pull qwen2.5:3b` (light, ~4 GB free RAM) or `qwen2.5:7b` (smarter) | free, local |
| `anthropic` | `ANTHROPIC_API_KEY` (`ANTHROPIC_MODEL`, default `claude-sonnet-5`) | billed per use |
| `none` | - | routed answers still work; free-form chat is off |

Semantic policy search (optional): `ollama pull nomic-embed-text` and set
`KB_EMBEDDING_MODEL=nomic-embed-text` in `.env`. Without it, keyword search (BM25) answers.

Other commands: `payroll-chatbot db upgrade` (migrations), `db status`, `db reset --yes`,
`rules load`, `kb` (knowledge base), `llm check`, `chat <email>` (terminal chat),
`check-config`.

## Demo users

| Code | Name | Login | Role |
|---|---|---|---|
| HR-001 | Anjali Mehta | anjali.mehta@example.com / Hr@12345 | Admin (hr) |
| HR-002 | Rohit Kapoor | rohit.kapoor@example.com / Hr@12345 | Admin (hr) - second approver |
| PM-001 | Kavita Rao | kavita.rao@example.com / Hr@12345 | Payroll Manager |
| PX-001 | Sameer Khan | sameer.khan@example.com / Hr@12345 | Payroll Executive |
| VW-001 | Neha Gupta | neha.gupta@example.com / Hr@12345 | Viewer |
| MGR-001 | Sneha Kapoor | sneha.kapoor@example.com / Manager@123 | Manager (Engineering) |
| EMP-101 | Rahul Verma | rahul.verma@example.com / Employee@123 | Employee - 1 unapproved absence in Sep |
| EMP-102 | Priya Nair | priya.nair@example.com / Employee@123 | Employee - approved CL + 2 absences in Sep |
| EMP-103 | Rohan Das | rohan.das@example.com / Employee@123 | Employee - ESIC |
| EMP-104 | Meena Iyer | meena.iyer@example.com / Employee@123 | Employee - old tax regime |
| EMP-105 | Kavita Reddy | kavita.reddy@example.com / Employee@123 | Employee - joined mid-September |
| INT-001 | Aditya Rao | aditya.rao@example.com / Employee@123 | Intern - PF not applicable |

Plus 50 generated employees `EMP-201` … `EMP-250` (7 departments, 9 states). Seed file:
`config/seed/chatbot_seed.yaml`. Change the demo passwords before sharing the app.

## How changes stay safe

- **Preview first**: every change (LOP, attendance, overtime, bonus / misc, salary) is
  simulated inside a database savepoint that is rolled back; you see before / after net pay.
- **Fingerprinted preview**: applying needs the preview id; if payroll data changed in the
  meantime, the approver sees today's numbers.
- **Maker-checker** (switchable by Admin): a second user with the Approve permission applies it.
- **Undo**: each applied change stores its exact reverse; undo is a new, audited change.
- **Locked months**: a finalized month cannot change until it is reopened (with a reason).
- **Audit log** for every change, login, report download and chat tool call (never passwords).
- **Grounded chat**: if the model writes a number no tool returned, the answer is replaced
  by the tool facts.

## How the chat works (LangGraph + LangChain + RAG)

The chat is a **LangGraph** state graph (`src/payroll_copilot/chat/agent.py`):

```
START -> route --(answered by the intent router)------------> finish -> END
           +--> agent <--> tools  (at most LLM_MAX_STEPS model calls)
                  +--> ground -------------------------------> finish -> END
```

| Node | What it does |
|---|---|
| `route` | Deterministic intent router: most payroll questions are answered from the engine without the LLM |
| `agent` | **LangChain** chat model (`ChatOllama` or `ChatAnthropic`) with the user's role's tools bound |
| `tools` | LangChain `StructuredTool`s; role check, validation and audit in `chat/tools.py`; UI cards travel as tool artifacts |
| `ground` | A number the model wrote that no tool returned replaces its text with the tool facts |
| `finish` | Keeps the last 10 exchanges in LangGraph's checkpointer memory (rebuilt from saved history after a restart) |

**RAG**: policy documents in `knowledge_base/` are split with LangChain's
`MarkdownHeaderTextSplitter` + `RecursiveCharacterTextSplitter`. `PolicyRetriever`
(a LangChain `BaseRetriever`, `services/policy_retriever.py`) fuses keyword search
(BM25) with optional semantic search (Ollama embeddings stored per chunk) by reciprocal
rank fusion, filtered by the reader's role and the documents' effective dates. The model
reaches it through the `search_policy_documents` tool and quotes passages with sources.

### Payroll run with approval (LangGraph human-in-the-loop)

*"October 2026 payroll chalao"* / *"... ka payroll process karo"* starts a LangGraph workflow
(`services/payroll_flow.py`) that **pauses for HR** with `interrupt`:

```
prechecks -> LOP review -> [wait: HR approves / re-checks / cancels] -> run
          -> [wait: background worker calculates] -> validate -> summary
```

A FAILED pre-check blocks the run until it is fixed and re-checked. The paused state is
saved by a LangGraph SQLite checkpointer (`data/checkpoints.db`), so a run waiting for
approval survives an API restart. Approving / finalizing stays on Payroll Runs
(maker-checker). API: `POST /payroll-flows`, `GET /payroll-flows/{id}`,
`POST /payroll-flows/{id}/resume`.

### Streaming answers

The chat uses `POST /chat/stream` (server-sent events): progress (*Looking up the
payslip…*, *Reading policy documents…*), then the answer word by word, then the final
checked reply. A number that no tool returned is never shown, not even while streaming
(it is held back until checked).

## Bulk payroll

*"September 2026 ka payroll sab employees ka process karo"* (or by department / state)
creates a batch: employees with absences without approved leave get a **bulk LOP card**
(per-employee LOP, total deduction, reason). The run is a database-backed queue in the API
(no Redis / Celery): chunks of 50, each employee in its own transaction, a double click
runs nothing twice, a restart resumes, failures can be retried alone, CSV report at the end.

## Payroll engine

`src/payroll_copilot/calculations` + `config/rules`: salary proration, PF / EPS / EDLI
(effective-dated: ₹15,000 ceiling until 31-Aug-2026, ₹25,000 from 01-Sep-2026), ESIC, PT
(all states), LWF, TDS (old / new regime, age categories, 80C / 80D / 80CCD(1B) / 80TTA
limits), overtime (`config/payroll/overtime.yaml`), arrears and F&F, each with a
calculation trace. Money is `Decimal` only. **All rule values are seed values marked
UNVERIFIED - verify them before real use.**

## Login and security

- JWT (HS256, `JWT_EXPIRE_MINUTES`, default 8 h); every request reloads the user, so a
  disabled login or changed role stops old tokens immediately.
- Passwords: bcrypt; same message and timing for a wrong email or password.
- `JWT_SECRET` is required outside localhost (≥ 32 characters).
- Knowledge base uploads that look like personal data (PAN, Aadhaar, bank account) are refused.

## Layout

```
config/rules/        versioned statutory rules (YAML)
config/payroll/      salary heads, structure templates, overtime policy
config/seed/         demo company
alembic/             migrations 0001-0010 (SQLite now, PostgreSQL-ready models)
src/payroll_copilot/
  core/          config, money (Decimal), dates, exceptions
  calculations/  deterministic payroll engine
  rules/         rule loader and resolver
  db/            models, session, seed
  auth/          passwords, JWT, roles and permissions
  repositories/  DB -> engine inputs
  services/      payroll, batches, actions / approvals / undo, audit, reports, leave,
                 tax help, knowledge base, AI audit, compliance
  chat/          intent router, routed answers, LangChain tools, LangGraph chat graph
  llm/           Ollama and Claude providers
  api/           FastAPI app
frontend/        React 19 + TypeScript + Tailwind 4 (Vite)
tests/           785 tests (engine, rules, API, chat routing, batches, approvals, reports, leave)
```

Checks: `pytest`, `ruff check .`, `cd frontend; npx tsc --noEmit -p .; npm run build`.
