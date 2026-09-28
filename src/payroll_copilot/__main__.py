"""Command line entry point: ``python -m payroll_copilot`` or ``payroll-copilot``."""

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select

from payroll_copilot import __version__
from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import ConfigurationError, PayrollCopilotError
from payroll_copilot.core.logging import get_logger, setup_logging
from payroll_copilot.domain.enums import RuleType

_RULE_META_COLUMNS = {
    "id", "created_at", "updated_at", "content_hash", "rule_code", "version", "name",
    "effective_from", "effective_to", "status", "applicability", "source_ref", "notes",
}  # fmt: skip


def _check_config() -> int:
    try:
        settings = Settings()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    setup_logging(settings)
    print(f"{settings.app_name} v{__version__}\n")
    print("Configuration (secrets masked):")
    summary = settings.summary()
    width = max(len(key) for key in summary)
    for key, value in summary.items():
        print(f"  {key:<{width}}  {value}")

    issues = settings.config_issues()
    print()
    if issues:
        print("Warnings:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("No configuration issues found.")
    if not settings.llm_enabled:
        print("  - LLM disabled (LLM_PROVIDER=none): deterministic features only.")

    get_logger(__name__).debug("Config check complete")
    return 0


def _db_upgrade() -> int:
    from payroll_copilot.db.migrate import upgrade_to_head

    upgrade_to_head()
    print(f"Database migrated to head: {get_settings().database_url}")
    return 0


def _rules_load() -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.rules.loader import load_rule_files, sync_rules

    files = load_rule_files(get_settings().rules_dir)
    with session_scope() as session:
        report = sync_rules(session, files)
    if report.inserted or report.updated:
        from payroll_copilot.audit.logger import AuditLogger

        AuditLogger().log("rules.sync", entity="rules", details={
            "inserted": report.inserted, "updated": report.updated,
            "unchanged": len(report.unchanged),
        })  # fmt: skip
    print(
        f"Rules: {len(files)} files, {report.total} versions "
        f"(inserted {len(report.inserted)}, updated {len(report.updated)}, "
        f"unchanged {len(report.unchanged)})"
    )
    return 0


def _db_seed() -> int:
    from payroll_copilot.db.seed import seed_demo_data
    from payroll_copilot.db.session import session_scope

    _db_upgrade()
    _rules_load()
    with session_scope() as session:
        report = seed_demo_data(session)
    if report.skipped:
        added = report.counts.get("salary_heads")
        extra = f" (added {added} new salary heads)" if added else ""
        print(f"Demo data already present - nothing seeded{extra}.")
        return 0
    print("Seeded: " + ", ".join(f"{k}={v}" for k, v in report.counts.items()))
    print("\nEmployee scenarios:")
    for code, scenario in report.scenarios.items():
        print(f"  {code:<9} {scenario}")
    return 0


def _db_reset(confirmed: bool) -> int:
    settings = get_settings()
    if not settings.is_sqlite:
        print("db reset only supports SQLite dev databases.")
        return 1
    if not confirmed:
        print(f"This deletes {settings.database_url}. Re-run with --yes to confirm.")
        return 1
    from payroll_copilot.db.session import get_engine

    get_engine().dispose()
    path = Path(settings.database_url.removeprefix("sqlite:///"))
    path.unlink(missing_ok=True)
    print(f"Deleted {path}")
    return _db_seed()


def _db_status() -> int:
    import payroll_copilot.db.models  # noqa: F401
    from payroll_copilot.db.base import Base
    from payroll_copilot.db.session import get_engine, session_scope

    existing = set(inspect(get_engine()).get_table_names())
    with session_scope() as session:
        for name, table in sorted(Base.metadata.tables.items()):
            if name in existing:
                count = session.scalar(select(func.count()).select_from(table))
            else:
                count = "-"
            print(f"  {name:<28} {count}")
    return 0


def _rules_resolve(rule_type: str, period: str, employee_code: str) -> int:
    from payroll_copilot.db.models import Employee
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.rules.applicability import describe
    from payroll_copilot.rules.context import build_employee_context
    from payroll_copilot.rules.service import RuleService

    payroll_period = PayrollPeriod.parse(period)
    with session_scope() as session:
        employee = session.scalar(select(Employee).where(Employee.employee_code == employee_code))
        if employee is None:
            print(f"Employee {employee_code} not found")
            return 1
        context = build_employee_context(employee, payroll_period)
        resolved = RuleService(session).resolve(RuleType(rule_type), payroll_period, context)
        rule = resolved.rule
        who = f"{employee_code} ({employee.full_name})"
        print(f"{rule_type} rule for {who} in {payroll_period.label}")
        print(f"  Rule        : {resolved.reference}")
        print(f"  Name        : {rule.name}")
        print(f"  Status      : {rule.status.value}")
        print(f"  Applies when: {describe(rule.applicability)}")
        print(f"  Source      : {rule.source_ref}")
        print("  Parameters  :")
        for column in type(rule).__table__.columns:
            if column.key not in _RULE_META_COLUMNS:
                value = getattr(rule, column.key)
                print(f"    {column.key:<28} {getattr(value, 'value', value)}")
        for slab in getattr(rule, "slabs", []):
            band = f"{slab.lower_exclusive or 0} < wage <= {slab.upper_inclusive or '∞'}"
            amount = getattr(slab, "amount", None)
            shown = f"₹{amount}" if amount is not None else f"{slab.rate}%"
            gender = getattr(slab, "gender", None)
            extra = f" gender={gender.value}" if gender else ""
            overrides = getattr(slab, "month_overrides", None)
            extra += f" overrides={overrides}" if overrides else ""
            print(f"    slab: {band:<32} {shown}{extra}")
        for warning in resolved.warnings:
            print(f"  WARNING: {warning}")
    return 0


def _salary_show(employee_code: str, period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.salary_structure_service import SalaryStructureService

    with session_scope() as session:
        view = SalaryStructureService(session).get_salary_structure(
            employee_code, PayrollPeriod.parse(period)
        )
    print(view.to_text())
    return 0


def _salary_calc(employee_code: str, period: str, show_trace: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.salary_service import SalaryService

    with session_scope() as session:
        view = SalaryService(session).calculate(employee_code, PayrollPeriod.parse(period))
    print(view.to_text())
    if show_trace:
        print()
        print(view.salary.trace.to_text())
    return 0


def _pf_calc(employee_code: str, period: str, show_trace: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.pf_service import PFService, render_pf

    with session_scope() as session:
        result = PFService(session).calculate(employee_code, PayrollPeriod.parse(period))
    print(render_pf(result, employee_code, period))
    if show_trace and result.trace:
        print()
        print(result.trace.to_text())
    return 0


def _pf_compare(employee_code: str, period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.pf_service import PFService, render_comparison

    with session_scope() as session:
        rows = PFService(session).compare_rule_versions(employee_code, PayrollPeriod.parse(period))
    print(render_comparison(rows, employee_code, period))
    return 0


def _esic_calc(employee_code: str, period: str, show_trace: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.esic_service import ESICService, render_esic

    with session_scope() as session:
        result = ESICService(session).calculate(employee_code, PayrollPeriod.parse(period))
    print(render_esic(result, employee_code, period))
    if show_trace and result.trace:
        print()
        print(result.trace.to_text())
    return 0


def _pt_calc(employee_code: str, period: str, show_trace: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.pt_service import PTService, render_pt

    with session_scope() as session:
        result = PTService(session).calculate(employee_code, PayrollPeriod.parse(period))
    print(render_pt(result, employee_code, period))
    if show_trace and result.trace:
        print()
        print(result.trace.to_text())
    return 0


def _lwf_calc(employee_code: str, period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.lwf_service import LWFService, render_lwf

    with session_scope() as session:
        result = LWFService(session).calculate(employee_code, PayrollPeriod.parse(period))
    print(render_lwf(result, employee_code, period))
    return 0


def _tds_calc(
    employee_code: str, period: str, show_trace: bool, override: str | None, reason: str | None
) -> int:
    from payroll_copilot.core.money import to_decimal
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.tds_service import TDSService, render_tds

    with session_scope() as session:
        result = TDSService(session).calculate(
            employee_code,
            PayrollPeriod.parse(period),
            override_amount=to_decimal(override) if override is not None else None,
            override_reason=reason,
        )
    print(render_tds(result, employee_code, period))
    if show_trace and result.trace:
        print()
        print(result.trace.to_text())
    return 0


def _arrear_calc(employee_code: str, period: str) -> int:
    from payroll_copilot.core.money import format_inr
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.salary_service import SalaryService

    with session_scope() as session:
        view = SalaryService(session).calculate(employee_code, PayrollPeriod.parse(period))
    arrears = view.arrears
    if arrears is None:
        print(f"Arrears | {employee_code} | {period}: no arrears paid in this period")
        return 0
    print(f"Arrears paid in {PayrollPeriod.parse(period).label} | {employee_code}")
    for month in arrears.months:
        print(f"  Month {month.period}: paid {month.paid_days}/{month.basis_days} days")
        for line in month.lines:
            print(f"    {line.name:<24} {line.formula:<40} {format_inr(line.amount):>10}")
        if month.pf_rule:
            impact = ", ".join(f"{k} {format_inr(v)}" for k, v in month.pf_impact.items())
            print(f"    PF on arrear ({month.pf_rule}): {impact}")
            if month.pf_note:
                print(f"      {month.pf_note}")
    print(f"  Total arrear {format_inr(arrears.total)}")
    for c in arrears.comparisons:
        uploaded = format_inr(c.uploaded) if c.uploaded is not None else "not uploaded"
        print(f"  {c.head_code:<10} expected {format_inr(c.expected):>10} | uploaded "
              f"{uploaded:>12} | {c.status.value}")  # fmt: skip
    for w in arrears.warnings:
        print(f"  WARNING: {w}")
    return 0


def _fnf_calc(employee_code: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.fnf_service import FnFService

    with session_scope() as session:
        view = FnFService(session).calculate(employee_code)
    print(view.to_text())
    return 0


def _db_seed_actual(replace: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll

    with session_scope() as session:
        report = seed_actual_payroll(session, replace=replace)
    if not report.created:
        print(f"Actual payroll for {report.period} already exists (use --replace to rebuild).")
        return 0
    from payroll_copilot.audit.logger import AuditLogger

    AuditLogger().log("payroll.seed_actual", entity="payroll_run", entity_id=report.period,
                      details={"employees": report.employees, "replace": replace,
                               "injected": sorted(report.injected)})  # fmt: skip
    print(f"Actual payroll {report.period}: {report.employees} payslips (local demo data)")
    for code, note in report.injected.items():
        print(f"  injected  {code:<9} {note}")
    return 0


def _validate_employee(employee_code: str, period: str, show_all: bool) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.validation_service import ValidationService

    with session_scope() as session:
        result = ValidationService(session).validate_employee(
            employee_code, PayrollPeriod.parse(period)
        )
    print(result.to_text(only_issues=not show_all))
    return 0


def _validate_payroll(period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.validation_service import ValidationService

    with session_scope() as session:
        result = ValidationService(session).validate_period(PayrollPeriod.parse(period))
    print(result.to_text())
    return 0


def _kb(args) -> int:
    from payroll_copilot.rag.service import KnowledgeBase

    kb = KnowledgeBase()
    command = args.kb_command
    if command == "generate":
        paths = kb.generate()
        print(f"Generated {len(paths)} rule documents under {kb.kb_root}")
        return 0
    if command == "ingest":
        if args.generate:
            print(f"Generated {len(kb.generate())} rule documents")
        report = kb.ingest(rebuild=args.rebuild)
        print(f"Ingested {report.documents} documents, {report.chunks} chunks: {report.per_module}")
        print(f"Vector store: {kb.store.count()} chunks at {kb.settings.vector_store_dir}")
        return 0
    period = PayrollPeriod.parse(args.period) if args.period else None
    if command == "search":
        chunks = kb.search(
            args.query, module=args.module, period=period, state=args.state, k=args.k
        )
        if not chunks:
            print("No matching documents for these filters.")
        for i, chunk in enumerate(chunks, start=1):
            print(f"[{i}] score {chunk.score:.4f} | {chunk.citation}")
            print("    " + chunk.text[:300].replace("\n", "\n    "))
        return 0
    answer = kb.ask(args.query, module=args.module, period=period, state=args.state, k=args.k)
    print(answer.answer)
    if answer.sources:
        print("\nSources:")
        for source in answer.sources:
            print(f"  {source}")
    for w in answer.warnings:
        print(f"WARNING: {w}")
    return 0


def _tools(args) -> int:
    import json

    from payroll_copilot.tools import build_tools

    tools = {t.name: t for t in build_tools()}
    if args.tools_command == "list":
        for tool in tools.values():
            params = ", ".join(tool.args)
            print(f"{tool.name:<24} [{tool.metadata['access']}] ({params})")
        return 0
    tool = tools.get(args.name)
    if tool is None:
        print(f"Unknown tool {args.name}; see `payroll-copilot tools list`")
        return 1
    content = tool.invoke(json.loads(args.args))
    print(content)
    return 1 if str(content).startswith("ERROR") else 0


def _print_reply(reply, verbose: bool) -> None:
    print(reply.answer)  # includes sources and warnings
    if verbose:
        print(f"\n[intent={reply.intent} route={reply.route} tools={reply.tools_used} "
              f"entities={ {k: v for k, v in reply.entities.items() if v} } "
              f"llm={reply.used_llm}]")  # fmt: skip


def _ask(question: str, thread: str, verbose: bool) -> int:
    from payroll_copilot.agent.copilot import PayrollCopilot

    reply = PayrollCopilot().ask(question, thread_id=thread)
    _print_reply(reply, verbose)
    return 0


def _chat(verbose: bool) -> int:
    from payroll_copilot.agent.copilot import PayrollCopilot

    copilot = PayrollCopilot()
    print("AI Payroll Copilot - type 'exit' to quit (local data only)")
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in {"exit", "quit", "bye"}:
            break
        if question:
            print()
            _print_reply(copilot.ask(question, thread_id="cli"), verbose)
    return 0


def _qa(args) -> int:
    from pathlib import Path

    from payroll_copilot.core.money import format_inr
    from payroll_copilot.db.session import read_scope, session_scope
    from payroll_copilot.services.qa_service import QAService

    if args.qa_command == "run":
        period = PayrollPeriod.parse(args.period)
        with read_scope() as session:
            report, text, _ = QAService(session).run(period)
        rendered = {"text": text, "md": report.to_markdown(), "csv": report.to_csv()}[args.format]
        if args.out:
            Path(args.out).write_text(rendered, encoding="utf-8")
            print(f"QA report written to {args.out}")
        else:
            print(rendered)
        if args.save:  # saving is a write action -> approval request, never a direct save
            from payroll_copilot.audit.context import current_actor
            from payroll_copilot.services.approval_service import ApprovalService

            with session_scope() as session:
                view = ApprovalService(session).request(
                    "SAVE_QA_RUN", {"period": str(period)}, requested_by=current_actor()
                )
            print(f"\nApproval request #{view.id} created to save this QA run. An approver must "
                  f"run: payroll-copilot approval approve {view.id} --as <approver>")  # fmt: skip
        return 0

    with read_scope() as session:
        service = QAService(session)
        if args.qa_command == "history":
            runs = service.history()
            if not runs:
                print("No saved QA runs.")
            for run in runs:
                print(f"  #{run.id:<4} {run.period}  {run.created_at:%Y-%m-%d %H:%M}  "
                      f"employees {run.employees_checked}  findings {run.issues_found}  "
                      f"{run.summary.get('by_severity', {})}")  # fmt: skip
            return 0
        run, issues = service.load(args.run_id)
        print(f"QA run #{run.id} | {run.period} | {run.status.value} | findings {run.issues_found}")
        for issue in issues:
            if issue.trace.get("primary"):
                diff = format_inr(issue.difference) if issue.difference is not None else "-"
                print(f"  {issue.component:<14} {issue.category or '-':<12} diff {diff:>10}  "
                      f"{issue.root_cause or ''}")  # fmt: skip
        return 0


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _api_serve(host: str | None, port: int | None) -> int:
    import uvicorn

    from payroll_copilot.api.main import create_app

    settings = get_settings()
    host = host or settings.api_host
    port = port or settings.api_port
    if host not in _LOOPBACK and settings.api_key is None:
        print(f"Refusing to listen on {host} without API_KEY; set API_KEY in .env or use "
              "127.0.0.1")  # fmt: skip
        return 1
    print(f"AI Payroll Copilot API on http://{host}:{port}  (docs: /docs, local data only)")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def _ui(with_api: bool, port: int) -> int:
    import subprocess
    import time

    import httpx

    from payroll_copilot.core.config import PROJECT_ROOT

    settings = get_settings()
    app_path = PROJECT_ROOT / "src" / "payroll_copilot" / "ui" / "streamlit_app.py"
    api_process = None
    if with_api:
        api_process = subprocess.Popen([sys.executable, "-m", "payroll_copilot", "api", "serve"],
                                       cwd=PROJECT_ROOT)  # fmt: skip
        for _ in range(60):
            try:
                httpx.get(f"{settings.api_base_url}/health", timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            api_process.terminate()
            print("API did not start; run `payroll-copilot api serve` to see the error")
            return 1
    print(f"UI on http://127.0.0.1:{port} (API {settings.api_base_url}); Ctrl+C to stop")
    try:
        return subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(app_path),
             "--server.address", "127.0.0.1", "--server.port", str(port),
             "--browser.gatherUsageStats", "false"],
            cwd=PROJECT_ROOT,
        )  # fmt: skip
    except KeyboardInterrupt:
        return 0
    finally:
        if api_process is not None:
            api_process.terminate()


def _same(session):
    """Session provider yielding an already open session (caller owns the transaction)."""
    from contextlib import nullcontext

    return nullcontext(session)


def _approval(args) -> int:
    from payroll_copilot.audit.context import audit_context, default_actor
    from payroll_copilot.db.session import read_scope, session_scope
    from payroll_copilot.services.approval_service import ApprovalService

    command = args.approval_command
    if command in ("list", "show"):
        with read_scope() as session:
            service = ApprovalService(session)
            views = [service.get(args.approval_id)] if command == "show" else service.list(
                args.status)  # fmt: skip
        if not views:
            print("No approval requests.")
        for view in views:
            print(view.to_text() if command == "show" else
                  f"  #{view.id:<4} {view.status:<9} {view.action_type:<22} {view.description}  "
                  f"(by {view.requested_by})")  # fmt: skip
        return 0

    user = getattr(args, "as_user", None) or default_actor().split(":", 1)[1]
    actor = f"cli:{user}"
    with audit_context(actor=actor), session_scope() as session:
        service = ApprovalService(session)
        if command == "approve":
            view = service.approve(args.approval_id, user, args.comment)
        elif command == "reject":
            view = service.reject(args.approval_id, user, args.comment or "")
        else:
            payloads = {
                "tds-override": ("TDS_OVERRIDE", lambda a: {
                    "employee_code": a.employee, "period": a.period, "amount": a.amount,
                    "reason": a.reason}),
                "attendance": ("ATTENDANCE_CORRECTION", lambda a: {
                    "employee_code": a.employee, "period": a.period, "lop_days": a.lop,
                    "reason": a.reason}),
                "qa-save": ("SAVE_QA_RUN", lambda a: {"period": a.period}),
                "verify-rule": ("VERIFY_RULE", lambda a: {
                    "rule_code": a.rule_code, "version": a.version, "evidence": a.evidence}),
            }  # fmt: skip
            action, build = payloads[args.kind]
            view = service.request(action, build(args), requested_by=actor)
    print(view.to_text())
    return 0 if view.status != "FAILED" else 1


def _memory(args) -> int:
    from payroll_copilot.agent.copilot import PayrollCopilot

    bot = PayrollCopilot(use_configured_llm=False)
    if args.memory_command == "clear":
        bot.clear(args.thread)
        print(f"Conversation '{args.thread}' forgotten.")
        return 0
    memory = bot.memory(args.thread)
    print(f"Thread '{args.thread}': {memory['turns']} turns | last employee "
          f"{memory['last_employee_code']} | last period {memory['last_period']}")  # fmt: skip
    for message in bot.history(args.thread)[-2 * args.last:]:
        text = message["content"].splitlines()[0][:120]
        print(f"  {message['role']:<9} {text}")
    return 0


def _audit(args) -> int:
    import json

    from payroll_copilot.audit.logger import AuditLogger

    logger = AuditLogger()
    if args.audit_command == "show":
        rows = [r for r in logger.search(limit=100000) if r["id"] == args.entry_id]
        if not rows:
            print(f"Audit entry #{args.entry_id} not found")
            return 1
        print(json.dumps(rows[0], indent=2, ensure_ascii=False))
        return 0
    rows = logger.search(action=args.action, entity_id=args.entity, limit=args.limit)
    if not rows:
        print("No audit entries.")
    for r in rows:
        what = r["tool_name"] or r["entity_id"] or ""
        extra = ""
        details = r["details"] or {}
        d = details
        if r["action"] == "chat.ask":
            extra = f"{d.get('intent')} {d.get('tools')} {d.get('rule_versions')}"
        elif r["action"] == "api.request":
            extra = f"{d.get('method')} {d.get('status')} {d.get('duration_ms')}ms"
        elif r["action"] == "tool.call":
            extra = f"ok={d.get('ok')} {d.get('rule_versions')}"
        head = f"  #{r['id']:<5} {r['ts'][:19]} {r['actor']:<20} {r['action']:<13}"
        print(f"{head} {what:<20} {extra}")
    return 0


def _llm_check() -> int:
    from payroll_copilot.llm.provider import check_llm

    status = check_llm()
    print(f"LLM provider: {status.provider} | model: {status.model} | ready: {status.ready}")
    print(f"  {status.message}")
    return 0


def _payroll_calc(employee_code: str, period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.payroll_service import PayrollCalculationService

    with session_scope() as session:
        view = PayrollCalculationService(session).calculate(
            employee_code, PayrollPeriod.parse(period)
        )
    print(view.to_text())
    return 0


def _salary_breakup(structure: str, gross: str) -> int:
    from payroll_copilot.calculations.structure import compute_structure_breakup
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.repositories.salary_repo import SalaryRepository

    with session_scope() as session:
        repo = SalaryRepository(session)
        result = compute_structure_breakup(
            repo.template(structure),
            gross,
            repo.heads(),
            rounding_mode=get_settings().default_rounding_mode,
        )
    print(result.to_text())
    return 0


def _attendance_show(employee_code: str, period: str) -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.attendance_service import AttendanceService

    with session_scope() as session:
        view = AttendanceService(session).get_attendance(employee_code, PayrollPeriod.parse(period))
    print(view.to_text())
    return 0


def _attendance_diagnose(employee_code: str, period: str, actual: str) -> int:
    from payroll_copilot.core.money import to_decimal
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.attendance_service import AttendanceService

    with session_scope() as session:
        diagnosis = AttendanceService(session).diagnose_paid_days(
            employee_code, PayrollPeriod.parse(period), to_decimal(actual)
        )
    print(f"Paid days | {employee_code} | {period}")
    print(f"  Expected  : {diagnosis.expected}")
    print(f"  Actual    : {diagnosis.actual}")
    print(f"  Difference: {diagnosis.difference}")
    print(f"  Status    : {diagnosis.status.value}")
    for cause in diagnosis.causes:
        print(f"  Possible cause: {cause}")
    print()
    print(diagnosis.expected_trace.to_text())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payroll-copilot", description="AI Payroll Copilot")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check-config", help="Show resolved configuration and problems")

    db = sub.add_parser("db", help="Database commands").add_subparsers(dest="db_command")
    db.add_parser("upgrade", help="Apply migrations")
    db.add_parser("seed", help="Migrate, load rules and seed demo data (idempotent)")
    db.add_parser("status", help="Row count per table")
    reset = db.add_parser("reset", help="Delete the SQLite DB and seed again")
    reset.add_argument("--yes", action="store_true", help="Confirm deletion")
    seed_actual = db.add_parser("seed-actual", help="Create the demo ACTUAL payroll run")
    seed_actual.add_argument("--replace", action="store_true", help="Rebuild if it exists")

    rules = sub.add_parser("rules", help="Rule commands").add_subparsers(dest="rules_command")
    rules.add_parser("load", help="Validate config/rules and sync them into the DB")
    resolve = rules.add_parser("resolve", help="Show the rule that applies to an employee")
    resolve.add_argument("rule_type", choices=[t.value for t in RuleType])
    resolve.add_argument("--period", required=True, help="YYYY-MM")
    resolve.add_argument("--employee", required=True, help="Employee code")

    salary = sub.add_parser("salary", help="Salary structure commands").add_subparsers(
        dest="salary_command"
    )
    show = salary.add_parser("show", help="Employee salary structure for a period")
    show.add_argument("employee", help="Employee code")
    show.add_argument("--period", required=True, help="YYYY-MM")
    calc = salary.add_parser("calc", help="Prorated earnings for a period (structure × paid days)")
    calc.add_argument("employee", help="Employee code")
    calc.add_argument("--period", required=True, help="YYYY-MM")
    calc.add_argument("--trace", action="store_true", help="Also print the full calculation trace")
    breakup = salary.add_parser("breakup", help="Split a gross using a structure template")
    breakup.add_argument("--structure", required=True, help="Structure code, e.g. STD-2026")
    breakup.add_argument("--gross", required=True, help="Monthly gross, e.g. 60000")

    attendance = sub.add_parser("attendance", help="Attendance / paid days").add_subparsers(
        dest="attendance_command"
    )
    att_show = attendance.add_parser("show", help="Attendance and paid-days trace")
    att_show.add_argument("employee", help="Employee code")
    att_show.add_argument("--period", required=True, help="YYYY-MM")
    diagnose = attendance.add_parser("diagnose", help="Why do actual paid days differ?")
    diagnose.add_argument("employee", help="Employee code")
    diagnose.add_argument("--period", required=True, help="YYYY-MM")
    diagnose.add_argument("--actual", required=True, help="Paid days in actual payroll")

    pf = sub.add_parser("pf", help="PF / EPS / EDLI / admin").add_subparsers(dest="pf_command")
    pf_calc = pf.add_parser("calc", help="PF components for an employee-month")
    pf_calc.add_argument("employee", help="Employee code")
    pf_calc.add_argument("--period", required=True, help="YYYY-MM")
    pf_calc.add_argument("--trace", action="store_true", help="Print the full PF trace")
    pf_compare = pf.add_parser("compare", help="Same earnings under every PF rule version")
    pf_compare.add_argument("employee", help="Employee code")
    pf_compare.add_argument("--period", required=True, help="YYYY-MM")

    esic = sub.add_parser("esic", help="ESIC").add_subparsers(dest="esic_command")
    esic_calc = esic.add_parser("calc", help="ESIC coverage and contributions")
    esic_calc.add_argument("employee", help="Employee code")
    esic_calc.add_argument("--period", required=True, help="YYYY-MM")
    esic_calc.add_argument("--trace", action="store_true", help="Print the full ESIC trace")

    pt = sub.add_parser("pt", help="Professional Tax").add_subparsers(dest="pt_command")
    pt_calc = pt.add_parser("calc", help="PT for an employee-month")
    pt_calc.add_argument("employee", help="Employee code")
    pt_calc.add_argument("--period", required=True, help="YYYY-MM")
    pt_calc.add_argument("--trace", action="store_true", help="Print the full PT trace")

    lwf = sub.add_parser("lwf", help="Labour Welfare Fund").add_subparsers(dest="lwf_command")
    lwf_calc = lwf.add_parser("calc", help="LWF for an employee-month")
    lwf_calc.add_argument("employee", help="Employee code")
    lwf_calc.add_argument("--period", required=True, help="YYYY-MM")

    tds = sub.add_parser("tds", help="Income tax (TDS)").add_subparsers(dest="tds_command")
    tds_calc = tds.add_parser("calc", help="Projected annual tax and monthly TDS")
    tds_calc.add_argument("employee", help="Employee code")
    tds_calc.add_argument("--period", required=True, help="YYYY-MM")
    tds_calc.add_argument("--trace", action="store_true", help="Print the full TDS trace")
    tds_calc.add_argument("--override", help="Manual monthly TDS (what-if; not saved)")
    tds_calc.add_argument("--reason", help="Reason for the override")

    arrear = sub.add_parser("arrear", help="Salary arrears").add_subparsers(dest="arrear_command")
    arrear_calc = arrear.add_parser("calc", help="Arrears paid in a period, with PF impact")
    arrear_calc.add_argument("employee", help="Employee code")
    arrear_calc.add_argument("--period", required=True, help="Payment period YYYY-MM")

    fnf = sub.add_parser("fnf", help="Full & final settlement").add_subparsers(dest="fnf_command")
    fnf_calc = fnf.add_parser("calc", help="F&F settlement for a separated employee")
    fnf_calc.add_argument("employee", help="Employee code")

    validate = sub.add_parser("validate", help="Expected vs actual").add_subparsers(
        dest="validate_command"
    )
    v_emp = validate.add_parser("employee", help="Validate one employee's payslip")
    v_emp.add_argument("employee", help="Employee code")
    v_emp.add_argument("--period", required=True, help="YYYY-MM")
    v_emp.add_argument("--all", action="store_true", help="Show matching components too")
    v_pay = validate.add_parser("payroll", help="Validate the whole payroll run")
    v_pay.add_argument("--period", required=True, help="YYYY-MM")

    kb = sub.add_parser("kb", help="Policy knowledge base (RAG)").add_subparsers(dest="kb_command")
    kb.add_parser("generate", help="Generate rule documents from config/rules")
    kb_ingest = kb.add_parser("ingest", help="Load knowledge_base/ into the vector store")
    kb_ingest.add_argument("--rebuild", action="store_true", help="Clear the store first")
    kb_ingest.add_argument("--generate", action="store_true", help="Regenerate rule docs first")
    for name, text in (("search", "Retrieve matching chunks"), ("ask", "Answer with sources")):
        p = kb.add_parser(name, help=text)
        p.add_argument("query", help="Question or search text")
        p.add_argument("--module", help="pf, esic, pt, lwf, tds, payroll, attendance, ...")
        p.add_argument("--period", help="Payroll period YYYY-MM (only rules effective then)")
        p.add_argument("--state", help="Work state code for PT/LWF, e.g. MH")
        p.add_argument("-k", type=int, default=4, help="Number of chunks")

    tools = sub.add_parser("tools", help="LangChain tools").add_subparsers(dest="tools_command")
    tools.add_parser("list", help="List tools and their arguments")
    tool_run = tools.add_parser("run", help="Invoke a tool with JSON arguments")
    tool_run.add_argument("name", help="Tool name")
    tool_run.add_argument(
        "--args", default="{}", help='JSON, e.g. \'{"employee_code": "EMP-002"}\''
    )

    api = sub.add_parser("api", help="FastAPI backend").add_subparsers(dest="api_command")
    serve = api.add_parser("serve", help="Run the API server (uvicorn)")
    serve.add_argument("--host", help="Default API_HOST (127.0.0.1)")
    serve.add_argument("--port", type=int, help="Default API_PORT (8000)")

    approval = sub.add_parser("approval", help="Maker-checker approvals for write actions")
    ap = approval.add_subparsers(dest="approval_command")
    ap_list = ap.add_parser("list", help="Approval requests")
    ap_list.add_argument("--status", help="PENDING | EXECUTED | REJECTED | FAILED")
    ap_show = ap.add_parser("show", help="One approval request")
    ap_show.add_argument("approval_id", type=int)
    for name in ("approve", "reject"):
        p = ap.add_parser(name, help=f"{name.title()} a pending request (approvers only)")
        p.add_argument("approval_id", type=int)
        p.add_argument("--as", dest="as_user", help="Approver user name (default: OS user)")
        p.add_argument("--comment", help="Required for reject")
    ap_req = ap.add_parser("request", help="Create an approval request").add_subparsers(dest="kind")
    request_args = {
        "tds-override": ("Override monthly TDS", ["employee"], ["period", "amount", "reason"]),
        "attendance": ("Correct LOP days", ["employee"], ["period", "lop", "reason"]),
        "qa-save": ("Save the QA run of a month", [], ["period"]),
        "verify-rule": ("Mark a rule version VERIFIED", ["rule_code", "version"], ["evidence"]),
    }
    for kind, (text, positional, options) in request_args.items():
        r = ap_req.add_parser(kind, help=text)
        for name in positional:
            r.add_argument(name, type=int if name == "version" else str)
        for name in options:
            r.add_argument(f"--{name}", required=True)
    for p in ap_req.choices.values():
        p.add_argument("--as", dest="as_user", help="Requester user name (default: OS user)")

    memory = sub.add_parser("memory", help="Conversation memory").add_subparsers(
        dest="memory_command"
    )
    mem_show = memory.add_parser("show", help="Stored turns and memory of a thread")
    mem_show.add_argument("thread", nargs="?", default="cli")
    mem_show.add_argument("--last", type=int, default=5, help="Show the last N turns")
    mem_clear = memory.add_parser("clear", help="Forget a thread")
    mem_clear.add_argument("thread", nargs="?", default="cli")

    audit = sub.add_parser("audit", help="Audit trail").add_subparsers(dest="audit_command")
    audit_list = audit.add_parser("list", help="Recent audit entries")
    audit_list.add_argument("--action", help="e.g. chat.ask, tool.call, api.request, qa.*")
    audit_list.add_argument("--entity", help="Entity id, e.g. a thread, employee or path")
    audit_list.add_argument("--limit", type=int, default=20)
    audit_show = audit.add_parser("show", help="One audit entry with details")
    audit_show.add_argument("entry_id", type=int)

    ui = sub.add_parser("ui", help="Streamlit chatbot UI (localhost)")
    ui.add_argument("--with-api", action="store_true", help="Also start the API server")
    ui.add_argument("--port", type=int, default=8501)

    qa = sub.add_parser("qa", help="Payroll QA agent").add_subparsers(dest="qa_command")
    qa_run = qa.add_parser("run", help="QA report for a payroll month")
    qa_run.add_argument("--period", required=True, help="YYYY-MM")
    qa_run.add_argument("--save", action="store_true", help="Save run + issues in the local DB")
    qa_run.add_argument("--format", choices=["text", "md", "csv"], default="text")
    qa_run.add_argument("--out", help="Write the report to this file")
    qa.add_parser("history", help="Saved QA runs")
    qa_show = qa.add_parser("show", help="Primary findings of a saved QA run")
    qa_show.add_argument("run_id", type=int)

    ask = sub.add_parser("ask", help="Ask the copilot one question")
    ask.add_argument("question", help="e.g. \"ARIF-112 ka PF September 2026 mein kitna hai?\"")
    ask.add_argument("--thread", default="cli", help="Conversation thread id")
    ask.add_argument("-v", "--verbose", action="store_true", help="Show intent/route/tools")
    chat = sub.add_parser("chat", help="Interactive copilot chat")
    chat.add_argument("-v", "--verbose", action="store_true", help="Show intent/route/tools")

    llm = sub.add_parser("llm", help="LLM provider").add_subparsers(dest="llm_command")
    llm.add_parser("check", help="Is the configured LLM reachable and pulled?")

    payroll = sub.add_parser("payroll", help="Expected payslip").add_subparsers(
        dest="payroll_command"
    )
    payroll_calc = payroll.add_parser("calc", help="Expected payslip for an employee-month")
    payroll_calc.add_argument("employee", help="Employee code")
    payroll_calc.add_argument("--period", required=True, help="YYYY-MM")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        sub_command = next(
            (
                getattr(args, name)
                for name in (
                    "db_command", "rules_command", "salary_command", "attendance_command",
                    "pf_command", "esic_command", "pt_command", "lwf_command", "tds_command",
                    "arrear_command", "fnf_command", "validate_command", "payroll_command",
                    "kb_command", "tools_command", "llm_command", "qa_command", "api_command",
                    "memory_command", "audit_command", "approval_command",
                )  # fmt: skip
                if getattr(args, name, None)
            ),
            None,
        )
        match args.command, sub_command:
            case "check-config", _:
                return _check_config()
            case "approval", ("list" | "show" | "approve" | "reject" | "request"):
                return _approval(args)
            case "memory", ("show" | "clear"):
                return _memory(args)
            case "audit", ("list" | "show"):
                return _audit(args)
            case "ui", _:
                return _ui(args.with_api, args.port)
            case "ask", _:
                return _ask(args.question, args.thread, args.verbose)
            case "chat", _:
                return _chat(args.verbose)
            case "db", "upgrade":
                return _db_upgrade()
            case "db", "seed":
                return _db_seed()
            case "db", "status":
                return _db_status()
            case "db", "reset":
                return _db_reset(args.yes)
            case "api", "serve":
                return _api_serve(args.host, args.port)
            case "qa", ("run" | "history" | "show"):
                return _qa(args)
            case "tools", ("list" | "run"):
                return _tools(args)
            case "llm", "check":
                return _llm_check()
            case "kb", ("generate" | "ingest" | "search" | "ask"):
                return _kb(args)
            case "db", "seed-actual":
                return _db_seed_actual(args.replace)
            case "validate", "employee":
                return _validate_employee(args.employee, args.period, args.all)
            case "validate", "payroll":
                return _validate_payroll(args.period)
            case "rules", "load":
                return _rules_load()
            case "rules", "resolve":
                return _rules_resolve(args.rule_type, args.period, args.employee)
            case "salary", "show":
                return _salary_show(args.employee, args.period)
            case "salary", "calc":
                return _salary_calc(args.employee, args.period, args.trace)
            case "salary", "breakup":
                return _salary_breakup(args.structure, args.gross)
            case "pf", "calc":
                return _pf_calc(args.employee, args.period, args.trace)
            case "pf", "compare":
                return _pf_compare(args.employee, args.period)
            case "esic", "calc":
                return _esic_calc(args.employee, args.period, args.trace)
            case "pt", "calc":
                return _pt_calc(args.employee, args.period, args.trace)
            case "lwf", "calc":
                return _lwf_calc(args.employee, args.period)
            case "tds", "calc":
                return _tds_calc(args.employee, args.period, args.trace, args.override, args.reason)
            case "arrear", "calc":
                return _arrear_calc(args.employee, args.period)
            case "fnf", "calc":
                return _fnf_calc(args.employee)
            case "payroll", "calc":
                return _payroll_calc(args.employee, args.period)
            case "attendance", "show":
                return _attendance_show(args.employee, args.period)
            case "attendance", "diagnose":
                return _attendance_diagnose(args.employee, args.period, args.actual)
    except PayrollCopilotError as exc:
        print(f"Error: {exc}")
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
