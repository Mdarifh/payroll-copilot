# AI Payroll Copilot

AI-powered payroll assistant: a deterministic, versioned-rule payroll calculation
engine with a LangChain/LangGraph copilot on top. The LLM never computes payroll
numbers; it only understands questions, picks tools and explains calculation traces.

> Status: **Phase 19 done - human approval (maker-checker) for every write action.**
>
> Data: everything runs on the local SQLite DB `data/payroll.db` with demo seed data.
> The project does not connect to any Zimyo system, database or API.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed automatically).

```powershell
uv sync                      # create .venv and install dependencies
copy .env.example .env       # then fill values (never commit .env)
.\.venv\Scripts\Activate.ps1
```

LLM (from Phase 13): free local model via [Ollama](https://ollama.com) -
`ollama pull qwen2.5:7b`.

## Run

```powershell
payroll-copilot check-config                 # resolved config (secrets masked)
payroll-copilot db seed                      # migrate + load rules + demo data (idempotent)
payroll-copilot db status                    # row count per table
payroll-copilot db reset --yes               # delete dev SQLite DB and seed again
payroll-copilot rules load                   # validate config/rules/** and sync to DB
payroll-copilot rules resolve PF --period 2026-09 --employee ARIF-112
payroll-copilot salary show ARIF-112 --period 2026-09     # structure + applicable compliance
payroll-copilot salary breakup --structure STD-2026 --gross 33333
payroll-copilot salary calc EMP-010 --period 2026-09 --trace       # prorated earnings
payroll-copilot pf calc ARIF-112 --period 2026-09 --trace          # PF components + trace
payroll-copilot pf compare ARIF-112 --period 2026-09               # old vs new PF rule
payroll-copilot esic calc EMP-008 --period 2026-09 --trace         # ESIC coverage + shares
payroll-copilot pt calc EMP-004 --period 2026-09 --trace           # PT (TN half-yearly)
payroll-copilot lwf calc EMP-009 --period 2026-09                  # LWF (HR monthly %)
payroll-copilot tds calc EMP-009 --period 2026-09 --trace          # projected tax + monthly TDS
payroll-copilot tds calc EMP-009 --period 2026-09 --override 3000 --reason "what-if"
payroll-copilot arrear calc EMP-004 --period 2026-09               # arrears + month-wise PF
payroll-copilot fnf calc EMP-006                                   # full & final settlement
payroll-copilot payroll calc ARIF-112 --period 2026-09             # full expected payslip
payroll-copilot db seed-actual                                     # demo ACTUAL run (with errors)
payroll-copilot validate payroll --period 2026-09                  # QA summary for the run
payroll-copilot validate employee EMP-002 --period 2026-09 [--all] # expected vs actual + causes
payroll-copilot kb ingest --generate --rebuild                     # rule docs + vector store
payroll-copilot kb search "PF wage ceiling" --module pf --period 2026-08
payroll-copilot kb ask "PT slab for women" --module pt --state MH --period 2026-09
payroll-copilot tools list                                          # 16 read-only tools
payroll-copilot tools run calculate_pf --args '{"employee_code": "ARIF-112", "period": "2026-09"}'
payroll-copilot llm check                                           # is Ollama running + model pulled?
payroll-copilot ask "ARIF-112 ka PF September 2026 mein kitna hai?" -v
payroll-copilot chat                                                # interactive (keeps context)
payroll-copilot qa run --period 2026-09                             # QA report (read-only)
payroll-copilot qa run --period 2026-09 --save                      # ... and store in qa_runs
payroll-copilot qa run --period 2026-09 --format csv --out qa.csv   # text | md | csv
payroll-copilot qa history / qa show 1
payroll-copilot api serve                                           # http://127.0.0.1:8000/docs
payroll-copilot ui --with-api                                       # http://127.0.0.1:8501 (UI + API)
payroll-copilot memory show cli / memory clear cli                  # conversation memory
payroll-copilot audit list --action chat.ask / audit show 7         # audit trail
payroll-copilot attendance show EMP-005 --period 2026-09            # paid-days trace
payroll-copilot attendance diagnose EMP-002 --period 2026-09 --actual 30
pytest --cov                                 # tests
ruff check .                                 # lint
```

## Layout

```
config/rules/{pf,esic,pt,lwf,tds}/*.yaml   versioned statutory rules (UNVERIFIED seed values)
config/seed/demo_company.yaml              demo company, heads, structures, 10 employees
alembic/                                   migrations (SQLite now, PostgreSQL ready)
src/payroll_copilot/
  core/        config, money (Decimal-only), dates, logging, exceptions
  domain/      shared enums
  db/          models (28 tables), portable DecimalType, session, migrate, seed
  rules/       YAML schemas, loader, applicability conditions, resolver, service
  calculations/ trace (CalculationResult), structure breakup, pay summary buckets
  repositories/ employee + salary data access (ORM -> domain objects)
  services/    salary structure view per employee and period
tests/unit/    core, db, rules, calculations, services, CLI
```

## Rule versioning

- One YAML file = one rule family (`rule_code`) with dated versions.
- The resolver picks exactly one version per employee + period, using effective
  dates and `applicability` conditions. Zero matches, overlaps and mid-month rule
  changes are explicit errors - it never guesses.
- `UNVERIFIED` rules work but add a warning to every trace; set
  `ALLOW_UNVERIFIED_RULES=false` to block them. `VERIFIED` rule values are
  immutable - add a new version instead.
- EPF: v1 = ₹15,000 ceiling until 31-Aug-2026, v2 = ₹25,000 ceiling from 01-Sep-2026.

## Paid days

`Paid days = payable days in the employed span − LOP days`

| Basis | Basis days | Full month | Partial month (DOJ/LWD inside month) |
|---|---|---|---|
| CALENDAR | days in month | days in month | employed calendar days |
| FIXED_30 / FIXED_26 | 30 / 26 | 30 / 26 | basis × employed / calendar days (**warns**: confirm policy) |
| WORKING_DAYS | month working days | working days | employed working days (needs work calendar) |

LOP reversal days are reported separately and paid as arrear at the earlier
month's rate - they are never added to the current month's paid days.

## Salary proration

- Prorated head = full-month amount × segment paid days / basis days, **rounded per
  line** (`DEFAULT_ROUNDING_MODE`); gross = Σ rounded lines. Non-prorated heads are
  paid in full.
- Salary change within a month → one segment per structure assignment. LOP is
  allocated by `DEFAULT_LOP_ALLOCATION` (`LAST_SEGMENT` or `PROPORTIONAL`) with a warning.
- LOP reversal → arrear lines at the previous month's structure and basis days.
- Every result explains the gap to the full-month gross: LOP, days not employed,
  mid-month revision, rounding.

## PF

All rates, ceilings, EPS age limit and rounding come from the PF rule version.

```
PF wage      = min(earned PF wage, ceiling)   (actual wage if pf_wage_restricted = false)
Employee PF  = PF wage × employee rate
EPS          = min(PF wage, EPS ceiling) × EPS rate    (0 if not eligible / age ≥ max)
Employer EPF = PF wage × employer rate − EPS
EDLI         = min(PF wage, EDLI ceiling) × EDLI rate
EPF admin    = PF wage × admin rate   (minimum is per establishment, not per employee)
```

## ESIC

- `esic_applicable = false` → employee/location not under ESIC.
- Coverage is tested once per contribution period (rule: Apr–Sep, Oct–Mar) on
  max(period start, DOJ) using the **full-month** ESIC wage rate (no arrears);
  covered employees stay covered until the period ends even after an increment.
- Contributions = earned ESIC wage (incl. arrears paid this month) × rule rates,
  rounded per rule (seed: CEIL).
- Not modelled yet: employee-share exemption for very low daily wages.

## PT and LWF

- PT rule per work state: levied or not, deduction months, slab wage basis
  (monthly / half-yearly / annual income), gender slabs, month overrides.
- Half-yearly/annual income = months with attendance are calculated; other months
  are estimated (past) or projected (future) from the salary structure, with a warning.
- A state without a configured rule is reported as **NOT CALCULATED** (never ₹0).
- LWF: fixed amounts or % of LWF wage with cap and employer multiplier, only in
  the rule's deduction months.

## TDS

- Rule per FY + regime (+ age for old regime): slabs, standard deduction, rebate
  and marginal relief, surcharge (with marginal relief), cess, section deduction
  limits and whether PT is deductible - all in `config/rules/tds/*.yaml`.
- FY salary = calculated months + structure estimates (past) + projections (future).
- TDS already deducted comes from actual payroll history; without history an even
  deduction for earlier months is assumed (warning). Leavers pay the balance in the
  final month. A TDS override is what-if in a calculation request, or permanent once an
  approved `TDS_OVERRIDE` request is executed (the trace names the approval).

## Arrears and F&F

- **As paid:** a month whose revision is paid later as arrear is calculated with the
  old amounts (what was actually paid then); the arrear appears as separate lines
  in the payment month. No double counting in half-yearly PT or annual TDS.
- Arrear = (new − old) × paid days / basis days of the arrear month.
- PF on arrears is month-wise with that month's PF rule and ceiling:
  PF(new salary) − PF(old salary). ESIC, PT and TDS count arrears in the payment month.
- Uploaded arrear amounts are compared with the expected amount per head.
- F&F: leave encashment and notice recovery use `FNF_*` policy settings; gratuity is
  flagged (no gratuity rule configured yet), not computed.

## Expected vs actual

- `config/seed/actual_payroll_2026_09.yaml` builds a demo ACTUAL run from the expected
  payslips plus documented deliberate errors (old PF ceiling, LOP not deducted, joiner
  paid full month, arrear / LOP reversal not paid, ESIC rounding, wrong PT slab,
  inconsistent header).
- Every component → Expected / Actual / Difference / Status (`COMPARISON_TOLERANCE`
  in ₹; days must match exactly) + issue type + root causes + expected trace.
- Root causes are recomputations that reproduce the actual value: other PF rule
  versions, other rounding modes, other PT slabs, the correct rule on the actual
  (wrong) wage (= UPSTREAM), LOP / DOJ / arrear amounts, and net-pay reconciliation.

## RAG knowledge base

```
knowledge_base/<module>/*.md|txt|pdf|docx  + <file>.meta.yaml (required metadata)
  → load → clean → chunk (Markdown headings kept) → local embeddings (BAAI/bge-small)
  → Chroma (data/chroma) → filter (module, period, IN / IN-<state>) → vector + BM25
  → RRF fusion + metadata rerank → answer with numbered sources
```

- Statutory rule documents are **generated** from `config/rules` (`kb generate`), one per
  rule version, so the assistant says exactly what the engine calculates.
- A period only ever retrieves documents effective in that period; nothing effective →
  "not found" (never an older/newer version).
- Without an LLM the answer is extractive (excerpts + sources). With an LLM the model
  must answer only from the numbered context; numbers not in the sources are flagged.
- The embedding model is downloaded once from HuggingFace into `data/models`; after that
  everything runs offline. Tests use deterministic fake embeddings (`-m slow` uses the
  real model if cached).

## LangChain tools and LLM

- 17 tools (`payroll_copilot.tools.build_tools`): employee, salary structure, attendance,
  actual payroll, expected payslip, salary / PF / PF-rule comparison / ESIC / PT / TDS /
  arrear / F&F calculations, expected-vs-actual, payroll-run validation, policy search.
- All tools are read-only (`metadata["access"] == "read"`, DB sessions always roll back),
  validate their arguments with Pydantic, mask PAN/UAN, and return
  `content_and_artifact`: short text for the LLM + JSON with values and traces.
- Domain errors come back as `ERROR: ...` text so the agent can recover or ask.
- LLM: `LLM_PROVIDER=ollama` + `LLM_MODEL=qwen2.5:7b` (free, local). Install Ollama and run
  `ollama pull qwen2.5:7b`; `payroll-copilot llm check` tells what is missing.
  With `LLM_PROVIDER=none` everything deterministic still works.

## LangGraph copilot

```
START → intent_detection → entity_extraction → route
  ├─ clarify (missing employee / month) → END
  ├─ knowledge → retrieve_rules → explanation → final_response → END
  ├─ general → general_agent (LLM tool-calling, or help) → final_response → END
  └─ attendance / salary / structure / payslip / PF / PF-compare / ESIC / PT / TDS /
     arrear / F&F / QA → retrieve_data → [retrieve_rules] → calculation → validation
     → explanation → final_response → END
```

- Intent and entities are rule-based (English + Hinglish; employee code or name; periods
  like `2026-09`, `Sep 2026`, `September 2026`), so the copilot works without an LLM.
- A thread remembers the last employee and month for follow-ups ("aur ESIC?").
  Memory is persistent (see Memory and audit).
- Write requests in chat ("EMP-009 ka TDS 2026-09 ke liye 3000 override karo") go to
  `prepare_action` → preview → the graph pauses (`interrupt`) → `yes` / `haan` submits an
  approval request (PENDING), `no` cancels. Chat never executes a write itself.
- The LLM (optional) only explains tool output; if its text contains a number that is
  not in the tool output, the deterministic explanation is used instead (warning shown).

## Payroll QA agent

`scope → validate → categorize → summarize` (LangGraph subgraph, read-only):
every employee is validated, component mismatches are grouped into **findings**
(one primary cause per category; dependent mismatches become effects), each with
severity, lead component, expected / actual / difference and a recommendation.
The report adds clean vs issue counts, net pay over/under-paid and statutory impact.
On the demo run it finds exactly the 8 injected errors from 64 component mismatches.
Saving (`--save` → `qa_runs` / `qa_issues`) creates a `SAVE_QA_RUN` approval request;
the run is stored only after an approver approves it.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | status, DB check, newest data month `latest_period` (no auth) |
| GET | `/employees`, `/employees/{code}` | employee list / master (PAN, UAN masked) |
| GET | `/employees/{code}/attendance?period=` | attendance + paid-days trace |
| GET | `/employees/{code}/salary-structure?period=` | structure + applicable compliance |
| GET | `/employees/{code}/payroll?period=` | ACTUAL payslip (404 if none) |
| GET | `/employees/{code}/payslip?period=` | EXPECTED payslip |
| POST | `/calculate/salary\|pf\|esic\|pt\|tds\|arrear\|payroll\|pf-rules` | `{employee_code, period}` |
| POST | `/calculate/fnf` | `{employee_code}` |
| POST | `/validate/employee`, `/validate/payroll` | expected vs actual / QA report (not saved) |
| GET | `/qa/runs`, `/qa/runs/{id}` | saved QA runs |
| POST | `/kb/search` | policy passages with citations |
| POST | `/chat` | copilot (thread memory) |
| GET | `/tools` | tools available to the agent |
| POST | `/approvals` | create a write request (X-User required) → 201 PENDING + preview |
| GET | `/approvals?status=`, `/approvals/{id}` | list / show requests |
| POST | `/approvals/{id}/approve\|reject` | decide (`{comment}`; reject needs one) |

- `X-API-Key` required when `API_KEY` is set (mandatory in prod). The server refuses a
  non-localhost host without an API key.
- Operation responses are `{text, data}`; money is always a string; traces included.
- Errors: 404 not found, 422 invalid input / rule problem, 503 configuration, 401 auth,
  403 not allowed to approve, 409 request already decided / not valid any more.
- The only writes are through `/approvals`.

## Streamlit UI

`payroll-copilot ui --with-api` starts the API and the UI on localhost. The UI only
calls the API (never the database directly).

- **Sidebar:** API/DB/LLM status, employee and payroll-month selectors (default = newest
  month with attendance data), acting user + token, new conversation.
- Results stay on screen across reruns (download, toggles) until the employee / month /
  calculation they belong to changes.
- **💬 Chat:** copilot with thread memory; the selected employee/month is added to a
  question only when it names neither (never to policy questions). Each answer shows
  route/tools/sources and the calculation traces as tables.
- **🧮 Calculation:** payslip (earnings / deductions / employer contributions / totals),
  salary, attendance, structure, PF, PF old vs new, ESIC, PT, TDS, arrears, F&F.
- **⚖️ Expected vs Actual:** component table with ✅ / ❌ status, issue type and root cause.
- **📋 QA Report:** metrics, findings by category chart, findings table, CSV download,
  "request save" (creates a `SAVE_QA_RUN` approval), saved runs.
- **📚 Policy:** period / module / state-filtered knowledge-base search with citations.
- **✅ Approvals:** pending requests with preview, approve / reject with comment (as the
  sidebar "Acting as" user), decided requests.

Streamlit usage statistics are disabled (`.streamlit/config.toml`).

## Memory and audit

- **Memory:** LangGraph `SqliteSaver` in `CHECKPOINT_DB` (`data/checkpoints.db`). A thread
  keeps its messages and last employee / month across restarts, so follow-ups work
  between separate `ask` commands, API restarts and UI sessions.
  `GET /chat/{thread}` shows it, `DELETE /chat/{thread}` / `memory clear` forgets it.
- **Audit (`audit_logs`):** `chat.ask` (question, intent, route, tools, rule versions,
  warnings), `tool.call` (args, ok/error, rule versions), `api.request` (method, path,
  status, duration), and admin actions `qa.save`, `rules.sync`, `payroll.seed_actual`.
  One request id links a chat turn to its tool calls (`X-Request-ID` header).
- Actor: `X-User` header for the API (validated), OS user for the CLI.
- PAN, 12-digit identifiers (UAN/Aadhaar) and secrets are masked; long text truncated.
- Audit is written in its own transaction. For reads a failed audit write is logged and
  ignored; for writes (`qa.save`) it is part of the same transaction - no audit, no save.
- `GET /audit`, `payroll-copilot audit list|show`. Disable with `AUDIT_ENABLED=false`.

## Conventions

- Money is always `Decimal`; floats are rejected. On SQLite money columns are
  stored as exact text, so compare them in Python, not in SQL.
- Rates are percentages (`Decimal("12")` = 12%).
- Payroll period format: `YYYY-MM`; financial year: `YYYY-YY` (e.g. `2026-27`).

## Approvals (maker-checker)

| Action | Preview | On approve |
|---|---|---|
| `TDS_OVERRIDE` | current vs new monthly TDS | stores an active `tds_overrides` row (replaces the old one) |
| `ATTENDANCE_CORRECTION` | LOP and paid days before / after | sets LOP (`source = APPROVAL#id`); fails if LOP changed since the request |
| `SAVE_QA_RUN` | findings count | runs QA again and saves it |
| `VERIFY_RULE` | rule status (must be UNVERIFIED) | marks the rule VERIFIED + stores the evidence; a later `rules sync` keeps it |

- `APPROVERS=["priya.hr","rahul.fin"]` in `.env`. No approvers → nothing can be approved.
- `USER_TOKENS={"priya.hr":"...","hr.user":"..."}`: when set, the API accepts `X-User` only
  with that user's `X-User-Token` (otherwise 401; audit actor `api:anonymous`), so nobody can
  approve by sending an approver's name. Required in prod when `APPROVERS` is set. The UI
  sends it from the sidebar "User token" field.
- The requester can never approve or reject their own request (the channel does not
  matter: `cli:alice` = `api:alice`). Reject needs a comment.
- Approval runs the action in a savepoint: any error → rolled back, status `FAILED` with
  the error. Request, execute and failure are audited (strict: no audit, no write).
- CLI: `approval request tds-override|attendance|qa-save|verify-rule ... --as USER`,
  `approval list [--status PENDING]`, `approval show ID`,
  `approval approve|reject ID --as USER --comment "..."`.
